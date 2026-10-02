"""MCP client: plug external tool servers into the agent's toolbox.

An MCP server is a small program (usually launched as a local subprocess,
spoken to over stdio) that exposes tools — "pause_tab", "query_db" — via
the Model Context Protocol. YAAH launches each registered server at
startup, asks it what tools it has, and merges them into the model's
tool menu under prefixed names (mcp_<server>_<tool>) so they can never
collide with built-ins or each other.

Trust model (user decision): REGISTRATION IS TRUST. A registered server
runs arbitrary code on this machine (or is contacted with your headers);
once registered, its tools are always available with no per-call
confirmation. Config lives in config.json "mcpServers" — command-style
entries keep Claude Desktop/Cursor configs copy-paste compatible, url
entries reach remote servers over streamable HTTP (legacy SSE opt-in via
"transport": "sse"). ${env:VAR} anywhere in a spec is substituted from
the environment at connect time, so tokens never sit resolved in the
config file:

    "mcpServers": {
        "browser": {"command": "npx", "args": ["-y", "@browser/mcp"]},
        "remote":  {"url": "https://mcp.example.com/mcp",
                    "headers": {"Authorization": "Bearer ${env:MCP_TOKEN}"}}
    }

The mcp SDK's stdio_client is an async context manager, so each session
lives inside a long-lived task owned by the manager; the event loop is
the FastAPI app's, started from main.lifespan.
"""
import asyncio
import logging
import os
import re

# Imported at module level (not lazily) so PyInstaller's static analysis
# sees them — bundling must not rely on --collect-all mcp, which pulls in
# mcp.server.cli and its optional typer dependency.
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# HTTP transports (issue #128): streamable HTTP is the modern standard
# (2025-03-26+); sse_client is the legacy fallback for pre-streamable
# servers. Both are context managers like stdio_client. The SDK manages
# its own httpx client lifecycle inside these context managers — we must
# never hand it an externally-created one (it wouldn't be closed, and
# the transport's background tasks hang process exit).
try:
    from mcp.client.streamable_http import streamable_http_client
    from mcp.client.sse import sse_client
    from mcp.shared._httpx_utils import create_mcp_http_client
except ImportError:  # pragma: no cover - older SDKs
    streamable_http_client = None
    sse_client = None
    create_mcp_http_client = None

log = logging.getLogger(__name__)

CALL_TIMEOUT = 60.0
SPAWN_TIMEOUT = 30.0
RESTART_BACKOFF = 5.0
# Exponential backoff (issue #128): failures that never converge must not
# retry blindly every 5s forever. After MAX_RESTARTS consecutive failures
# the server goes terminal ("failed (won't retry)") until the user retries.
MAX_RESTARTS = 8
BACKOFF_CAP = 30.0
_ENV_RE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def interpolate_env(value, getter=os.environ.get):
    """Recursively substitute ${env:VAR} in a spec's strings. Unknown vars
    become empty (shell-like), so a missing token fails loudly at the
    server instead of leaking the literal into a request."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: getter(m.group(1), "") or "", value)
    if isinstance(value, dict):
        return {k: interpolate_env(v, getter) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env(v, getter) for v in value]
    return value


def next_backoff(failures: int) -> float:
    """Exponential backoff with a cap: 5s, 10s, 20s, then 30s forever."""
    return min(RESTART_BACKOFF * (2 ** failures), BACKOFF_CAP)


class McpServerState:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.status = "starting"  # starting | connected | failed | stopped
        self.error = ""
        self.tools: list[dict] = []  # OpenAI-format schemas, prefixed
        self.protocol_version = ""  # negotiated MCP spec revision, surfaced in status
        self.failures = 0  # consecutive failed connection attempts
        self._session = None
        self._task: asyncio.Task | None = None
        self._generation = 0  # invalidates stale sessions after restarts

    def prefix(self, tool: str) -> str:
        return f"mcp_{self.name}_{tool}"

    def will_stop_retrying(self) -> bool:
        return self.failures >= MAX_RESTARTS

    def api_status(self) -> str:
        """Status label shown in the UI; terminal failures say so."""
        if self.status == "failed" and self.will_stop_retrying():
            return "failed (won't retry)"
        return self.status


class McpManager:
    def __init__(self):
        self.servers: dict[str, McpServerState] = {}

    # ------------------------------------------------------------ lifecycle

    def configured(self) -> dict:
        from backend.agent.config import load_config

        return load_config().get("mcpServers") or {}

    def start_all(self):
        """Launch every configured server (idempotent; called at startup
        and after config changes). Servers removed from config are stopped."""
        cfg = self.configured()
        for name in list(self.servers):
            if name not in cfg:
                self._stop(name)
        for name, spec in cfg.items():
            if not isinstance(spec, dict) or not (spec.get("command") or spec.get("url")):
                continue
            existing = self.servers.get(name)
            if (
                existing is not None
                and existing.spec == spec
                and existing.status in ("connected", "starting")
            ):
                continue  # unchanged and alive; keep the running session
            # A failed server with an unchanged spec restarts here too:
            # /api/mcp/reload is the user-facing "retry now" (fresh state,
            # failure budget reset). Stopped/terminal states relaunch.
            self._stop(name)
            state = McpServerState(name, interpolate_env(spec))
            self.servers[name] = state
            self._launch(state)

    def _launch(self, state: McpServerState):
        """Create the session task for a prepared state (seam for tests)."""
        state._task = asyncio.create_task(
            self._session_loop(state), name=f"mcp-{state.name}"
        )

    def _stop(self, name: str):
        state = self.servers.pop(name, None)
        if state:
            state.status = "stopped"
            if state._task:
                state._task.cancel()

    async def shutdown(self):
        """Stop every server, then await the cancelled session tasks so
        transports unroll (their context managers flush the httpx client
        and DELETE the remote session) before the loop closes."""
        tasks = [s._task for s in self.servers.values() if s._task]
        for name in list(self.servers):
            self._stop(name)
        for t in tasks:
            try:
                await asyncio.wait_for(asyncio.shield(t), 5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

    async def _session_loop(self, state: McpServerState):
        """Owns the transport + ClientSession context pair for this
        server; reconnects with exponential backoff until stopped (task
        cancelled) or the failure budget runs out."""
        while True:
            state._generation += 1
            gen = state._generation
            superseded = False
            give_up = False
            try:
                try:
                    async with self._open_transport(state) as (read, write):
                        async with ClientSession(read, write) as session:
                            await asyncio.wait_for(session.initialize(), SPAWN_TIMEOUT)
                            state._session = session
                            state.protocol_version = (
                                getattr(session, "protocol_version", "")
                                or getattr(getattr(session, "initialize_result", None),
                                           "protocol_version", "")
                                or ""
                            )
                            state.failures = 0
                            await self._discover(state, session)
                            state.status = "connected"
                            state.error = ""
                            # Park until the process exits or we're cancelled;
                            # any closed-transport error drops us to reconnect.
                            while state._generation == gen:
                                await asyncio.sleep(1.0)
                                await asyncio.wait_for(session.send_ping(), CALL_TIMEOUT)
                            return  # superseded by a restart/stop
                except* Exception as eg:  # noqa: BLE001 — a server dying must not die with us
                    if state._generation != gen:
                        superseded = True
                    else:
                        # ExceptionGroups bury the real error; surface every leaf.
                        leaves: list[str] = []
                        for exc in eg.exceptions:
                            if isinstance(exc, ExceptionGroup):
                                leaves.extend(
                                    f"{type(s).__name__}: {s}" for s in exc.exceptions
                                )
                            else:
                                leaves.append(f"{type(exc).__name__}: {exc}")
                        state.status = "failed"
                        state.error = "; ".join(leaves) or "unknown error"
                        state._session = None
                        state.failures += 1
                        give_up = state.will_stop_retrying()
                if give_up:
                    log.warning(
                        "MCP server %r failed %d times, giving up: %s",
                        state.name, state.failures, state.error)
                    return  # terminal; user retries via /api/mcp/reload
                if not superseded:
                    delay = next_backoff(state.failures - 1)
                    log.warning("MCP server %r failed: %s — retrying in %ss",
                                state.name, state.error, delay)
                    await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if superseded:
                return

    def _open_transport(self, state: McpServerState):
        """Context manager pair for the configured transport. stdio spawns
        a subprocess; streamable HTTP speaks to a URL (with legacy SSE
        fallback when the entry says so or the endpoint is .sse-style)."""
        spec = state.spec
        if spec.get("url"):
            url = spec["url"]
            headers = spec.get("headers") or None
            if spec.get("transport") == "sse":
                if sse_client is None:
                    raise RuntimeError("legacy SSE transport unavailable in this mcp SDK")
                return sse_client(url, headers=headers)
            if streamable_http_client is None:
                raise RuntimeError("streamable HTTP transport unavailable in this mcp SDK")
            # Headers via the SDK factory so the client stays SDK-owned
            # (see module comment): create_mcp_http_client applies
            # headers/timeouts and the context manager closes it.
            return streamable_http_client(
                url,
                http_client=create_mcp_http_client(
                    headers=headers or {},
                    timeout=float(spec.get("timeout", CALL_TIMEOUT)),
                ),
            )
        return stdio_client(
            StdioServerParameters(
                command=spec["command"],
                args=[str(a) for a in (spec.get("args") or [])],
                env=spec.get("env") or None,
            )
        )

    async def _discover(self, state: McpServerState, session):
        resp = await asyncio.wait_for(session.list_tools(), SPAWN_TIMEOUT)
        tools = []
        for t in resp.tools:
            # mcp 1.x/2.x renamed attrs (inputSchema -> input_schema); the
            # getattr pair keeps both server SDK generations working.
            schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": state.prefix(t.name),
                        "description": (
                            getattr(t, "description", None)
                            or f"MCP tool from server '{state.name}'"
                        ),
                        "parameters": schema
                        or {"type": "object", "properties": {}},
                    },
                }
            )
        state.tools = tools
        log.info("MCP server %r: %d tool(s)", state.name, len(tools))

    # ------------------------------------------------------------ access

    def schemas(self) -> list:
        """OpenAI-format schemas from every connected server, merged."""
        out = []
        for state in self.servers.values():
            if state.status == "connected":
                out.extend(state.tools)
        return out

    def find(self, prefixed: str) -> tuple[McpServerState | None, str]:
        """Split mcp_<server>_<tool> back into (state, tool). Server names
        may contain underscores, so match against known servers."""
        for state in self.servers.values():
            p = f"mcp_{state.name}_"
            if prefixed.startswith(p):
                return state, prefixed[len(p):]
        return None, ""

    async def call(self, prefixed: str, arguments: dict) -> dict:
        state, tool = self.find(prefixed)
        if state is None or state.status != "connected" or state._session is None:
            return {"error": f"MCP tool {prefixed} unavailable (server not connected)"}
        session = state._session
        try:
            resp = await asyncio.wait_for(
                session.call_tool(tool, arguments or {}), CALL_TIMEOUT
            )
        except asyncio.TimeoutError:
            return {"error": f"MCP tool {prefixed} timed out after {CALL_TIMEOUT:.0f}s"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

        # Content blocks: text is common; images are stored via imagedata
        # so the existing {"image": rel} contract attaches them for vision.
        out: dict = {}
        texts: list[str] = []
        image_rels: list[str] = []
        is_error = bool(
            getattr(resp, "is_error", None) or getattr(resp, "isError", False)
        )
        for block in resp.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                texts.append(block.text)
            elif kind == "image":
                from backend.agent.imagedata import save_bytes

                import base64

                # mcp 1.x/2.x renamed mimeType -> mime_type; either may appear.
                raw = base64.b64decode(block.data)
                mime = (
                    getattr(block, "mime_type", None)
                    or getattr(block, "mimeType", None)
                    or "image/png"
                )
                image_rels.append(save_bytes(raw, mime.split("/")[-1], "mcp"))
        if texts:
            joined = "\n".join(texts)
            try:
                import json as _json

                parsed = _json.loads(joined)
                if isinstance(parsed, dict):
                    out.update(parsed)
                else:
                    out["result"] = joined
            except Exception:  # noqa: BLE001 — plain text result
                out["result"] = joined
        if image_rels:
            out["images"] = image_rels
        if is_error:
            out["error"] = out.pop("result", "MCP tool reported an error")
        if not out:
            out["ok"] = True
        return out


manager = McpManager()
