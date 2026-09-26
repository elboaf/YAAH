"""Remote host session (LAN hosting).

One app, both roles. The CLIENT keeps everything authoritative here:
conversations, provider keys, the agent loop, skills. The HOST is a
stateless executor for workspace operations only — when a session is
active, workspace-touching tools are forwarded over HTTP to the host
and executed in the host's default workspace (its home directory).
Provider keys never leave the client; the only secret on the wire is
the user-chosen passphrase (header on every remote request).

Handshake/protocol: bump PROTOCOL_VERSION whenever the remote exec
contract changes; a client refuses to connect to a host advertising a
different protocol (clear "update both sides" error, per design).
"""

import json
import platform
import secrets
import socket
from pathlib import Path

import httpx

# Bump on any change to the remote endpoints' request/response shape.
# v4: adds expiring host-enforced conversation leases, revision-aware snapshot
# refresh/commit, and idempotent commit IDs.
# v3: adds authenticated read-only remote conversation metadata/history endpoints.
# v2: /api/remote/exec carries the client's selected workspace, and
# host-bound workspace paths are proxied raw (the client no longer
# normalizes them with its own OS's path rules).
PROTOCOL_VERSION = 4

# Random per-process identity: a host that is also a client can recognize
# itself at handshake time and refuse the self-connection (which would
# otherwise loop every tool/files call back through its own proxy).
INSTANCE_ID = secrets.token_hex(8)


def ensure_host_id() -> str:
    """Stable per-host identity, generated once and kept in config. Unlike
    INSTANCE_ID this survives restarts, so clients can scope their
    conversations to a host across reboots of either machine."""
    from backend.agent.config import load_config, save_config

    cfg = load_config().get("remote") or {}
    hid = cfg.get("host_id") or ""
    if not hid:
        hid = secrets.token_hex(8)
        save_config({"remote": {**cfg, "host_id": hid}})
    return hid


def ns_path(host_id: str, path: str | None) -> str:
    """Namespace a host workspace path so it can never collide with a local
    path on the client ('remote:<hid>:C:/repo'; host Default = 'remote:<hid>:' )."""
    return f"remote:{host_id}:{path or ''}"


def parse_ns(ws: str | None) -> tuple[str, str] | None:
    """Split a namespaced workspace back into (host_id, raw host path).
    None when the string isn't a remote workspace."""
    if ws and ws.startswith("remote:"):
        hid, _, path = ws[len("remote:"):].partition(":")
        return hid, path
    return None


# Workspace-touching tools: the only ones that execute remotely. Everything
# else (web tools, ask_user, load_skill, view_image) stays client-local.
REMOTE_TOOLS = {
    "bash",
    "powershell",
    "read_file",
    "write_file",
    "edit_file",
    "create_file",
    "delete_file",
    "move_file",
    "search_files",
    "git_status",
    "git_diff",
    "git_add",
    "git_commit",
    "git_push",
    "git_pull",
}

# Host shell tools may legitimately run up to 300s; leave headroom.
EXEC_TIMEOUT = 360.0


def host_info() -> dict:
    """What a connecting client learns from GET /api/remote/info."""
    windows = platform.system() == "Windows"
    return {
        "protocol": PROTOCOL_VERSION,
        "instance_id": INSTANCE_ID,
        "host_id": ensure_host_id(),
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_version": platform.release(),
        "machine": platform.machine(),
        "windows": windows,
        # The host's tools run in this (its Default workspace = home dir).
        "workspace_root": str(Path.home()),
    }


# The env line's shell caveat: on a cmd host the model's Unix reflexes
# (ls, grep, tail) each cost a failed turn before it falls back to
# findstr/Select-String — say so up front. Lives here because both the
# remote env_line and the local prompt (backend.agent.loop) use it.
CMD_TOOLS_NOTE = (
    "POSIX tools such as ls, grep, tail and head are NOT available there — "
    "use dir, findstr, or PowerShell Select-String / Get-Content -Tail instead."
)


class RemoteSession:
    """Client-side handle on a connected host. Holds only what the
    handshake returned plus the URL + passphrase; nothing is persisted
    backend-side (the frontend remembers hosts and reconnects)."""

    def __init__(self, url: str, passphrase: str, info: dict, app_version: str = ""):
        self.url = url.rstrip("/")
        self.passphrase = passphrase
        self.info = info
        self.app_version = app_version
        # Older peers (pre-host_id builds) still need a stable scope key;
        # derive one from the hostname so namespaces stay consistent.
        self.host_id = info.get("host_id") or (
            "h-" + (info.get("hostname") or self.url).lower().replace(" ", "-")
        )
        self.name = info.get("hostname") or self.url

    @property
    def windows(self) -> bool:
        return bool(self.info.get("windows"))

    def env_line(self, workspace: str = "") -> str:
        """Runtime-environment sentence for the system prompt: tools run on
        the HOST, so the model must use the host's OS/shell/paths, not the
        client's. The handshake only says windows/not-windows, so the shell
        is a guess — but the cmd caveat is what saves turns. When the chat
        has a workspace selected on this host, name it — otherwise the model
        reports the host's home as its cwd even mid-project."""
        i = self.info
        windows = bool(i.get("windows"))
        shell = "cmd.exe" if windows else "bash/sh"
        caveat = f" {CMD_TOOLS_NOTE}" if windows else ""
        ns = parse_ns(workspace)
        if ns is not None and ns[0] == self.host_id and ns[1]:
            ws = f"The workspace is {ns[1]} on the host"
        else:
            ws = (
                f"The workspace is the host's default workspace "
                f"({i.get('workspace_root', 'home')})"
            )
        return (
            f"Runtime environment: {i.get('os', '?')} {i.get('os_version', '')} "
            f"({i.get('machine', '?')}) on the remote host '{self.name}'. "
            f"The shell tool runs commands there through {shell};{caveat} use commands "
            f"and paths valid for THAT operating system. {ws}; "
            "file and shell tools operate there, not on this machine."
        )

    def _headers(self) -> dict:
        return {"X-Yaah-Remote": "1", "X-Yaah-Passphrase": self.passphrase}

    async def exec_tool(self, name: str, args: dict, workspace: str = "") -> dict:
        """Forward one workspace-tool call to the host, in `workspace` (a
        namespaced path for THIS host is stripped to the raw host path; a
        namespace for any OTHER host is rejected, mirroring the files-proxy
        defense). Never raises."""
        if name not in REMOTE_TOOLS:
            return {"error": f"{name} is not a workspace tool; it runs locally"}
        ns = parse_ns(workspace)
        if ns is not None:
            if ns[0] != self.host_id:
                return {"error": "that workspace belongs to a different remote host"}
            workspace = ns[1]
        try:
            async with httpx.AsyncClient(timeout=EXEC_TIMEOUT) as client:
                res = await client.post(
                    f"{self.url}/api/remote/exec",
                    json={"name": name, "args": args, "workspace": workspace},
                    headers=self._headers(),
                )
            if res.status_code == 401:
                return {"error": "remote host rejected the passphrase"}
            if res.status_code != 200:
                return {
                    "error": f"remote exec failed ({res.status_code}): {res.text[:200]}"
                }
            return res.json()
        except (httpx.HTTPError, OSError, json.JSONDecodeError) as e:
            return {"error": f"remote host unreachable: {type(e).__name__}: {e}"}

    async def proxy(self, method: str, path: str, *, params=None, json_body=None):
        """Proxy a files/attachments call to the host (same shape back)."""
        async with httpx.AsyncClient(timeout=EXEC_TIMEOUT) as client:
            res = await client.request(
                method,
                f"{self.url}{path}",
                params=params,
                json=json_body,
                headers=self._headers(),
            )
        return res


# Saved remote sessions keyed by stable host identity. `_active_host_id` is a
# temporary compatibility bridge for the legacy composer switcher and older
# callers; workspace-owner-aware code should call get_remote(host_id).
_sessions: dict[str, RemoteSession] = {}
_active_host_id: str | None = None


def register_remote(session: RemoteSession, *, make_active: bool = False) -> None:
    """Make a device available without changing local or other-host routing."""
    global _active_host_id
    # Production RemoteSession instances always have a stable ID. The fallback
    # preserves older lightweight test doubles and pre-registry integrations.
    host_id = getattr(session, "host_id", "legacy") or "legacy"
    _sessions[host_id] = session
    if make_active:
        _active_host_id = host_id


def registered_remotes() -> dict[str, RemoteSession]:
    """Snapshot of connected devices, keyed by stable host ID."""
    return dict(_sessions)


def unregister_remote(host_id: str) -> RemoteSession | None:
    """Remove one device; leave every other registered host untouched."""
    global _active_host_id
    removed = _sessions.pop(host_id, None)
    if _active_host_id == host_id:
        # A new UI connects devices without assigning a global active host;
        # when a legacy host goes away, fall back to another live session if
        # present instead of making local execution appear disconnected.
        _active_host_id = next(iter(_sessions), None)
    return removed


def get_remote(host_id: str | None = None) -> RemoteSession | None:
    """Get a specific device, or the legacy active device when omitted."""
    if host_id is not None:
        return _sessions.get(host_id)
    return _sessions.get(_active_host_id) if _active_host_id else None


def remote_for_workspace(workspace: str | None) -> RemoteSession | None:
    """Resolve the executor for a remote-namespaced workspace, if known."""
    parsed = parse_ns(workspace)
    if parsed is None:
        return None
    return get_remote(parsed[0])


def set_remote(session: RemoteSession) -> None:
    """Legacy connection behavior: register and make this device active."""
    register_remote(session, make_active=True)


def clear_remote(host_id: str | None = None) -> None:
    """Clear one registered device, or all devices for legacy teardown/tests."""
    global _active_host_id
    if host_id is not None:
        unregister_remote(host_id)
        return
    _sessions.clear()
    _active_host_id = None
