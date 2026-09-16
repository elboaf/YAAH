"""MCP client: plug external tool servers into the agent's toolbox.

An MCP server is a small program (usually launched as a local subprocess,
spoken to over stdio) that exposes tools — "pause_tab", "query_db" — via
the Model Context Protocol. YAAH launches each registered server at
startup, asks it what tools it has, and merges them into the model's
tool menu under prefixed names (mcp_<server>_<tool>) so they can never
collide with built-ins or each other.

Trust model (user decision): REGISTRATION IS TRUST. A registered server
runs arbitrary code on this machine; once registered, its tools are
always available with no per-call confirmation. Config lives in
config.json "mcpServers" (same shape as Claude Desktop/Cursor configs,
so ecosystem configs are copy-paste compatible):

    "mcpServers": {
        "browser": {"command": "npx", "args": ["-y", "@browser/mcp"]}
    }

The mcp SDK's stdio_client is an async context manager, so each session
lives inside a long-lived task owned by the manager; the event loop is
the FastAPI app's, started from main.lifespan.
"""
import asyncio
import logging

# Imported at module level (not lazily) so PyInstaller's static analysis
# sees them — bundling must not rely on --collect-all mcp, which pulls in
# mcp.server.cli and its optional typer dependency.
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger(__name__)

CALL_TIMEOUT = 60.0
SPAWN_TIMEOUT = 30.0
RESTART_BACKOFF = 5.0


class McpServerState:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.status = "starting"  # starting | connected | failed | stopped
        self.error = ""
        self.tools: list[dict] = []  # OpenAI-format schemas, prefixed
        self._session = None
        self._task: asyncio.Task | None = None
        self._generation = 0  # invalidates stale sessions after restarts

    def prefix(self, tool: str) -> str:
        return f"mcp_{self.name}_{tool}"


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
            if not isinstance(spec, dict) or not spec.get("command"):
                continue
            if name in self.servers and self.servers[name].spec == spec:
                continue  # unchanged; keep the running session
            self._stop(name)
            state = McpServerState(name, spec)
            self.servers[name] = state
            state._task = asyncio.create_task(
                self._session_loop(state), name=f"mcp-{name}"
            )

    def _stop(self, name: str):
        state = self.servers.pop(name, None)
        if state:
            state.status = "stopped"
            if state._task:
                state._task.cancel()

    async def shutdown(self):
        for name in list(self.servers):
            self._stop(name)

    async def _session_loop(self, state: McpServerState):
        """Owns the stdio_client + ClientSession context pair for this
        server; reconnects with backoff until stopped (task cancelled)."""
        while True:
            state._generation += 1
            gen = state._generation
            superseded = False
            try:
                try:
                    params = StdioServerParameters(
                        command=state.spec["command"],
                        args=[str(a) for a in (state.spec.get("args") or [])],
                        env=state.spec.get("env") or None,
                    )
                    async with stdio_client(params) as (read, write):
                        async with ClientSession(read, write) as session:
                            await asyncio.wait_for(session.initialize(), SPAWN_TIMEOUT)
                            state._session = session
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
                        log.warning("MCP server %r failed: %s — retrying in %ss",
                                    state.name, state.error, RESTART_BACKOFF)
                        await asyncio.sleep(RESTART_BACKOFF)
            except asyncio.CancelledError:
                return
            if superseded:
                return

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

                raw = base64.b64decode(block.data)
                image_rels.append(save_bytes(raw, block.mimeType.split("/")[-1], "mcp"))
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
