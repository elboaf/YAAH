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
PROTOCOL_VERSION = 1

# Random per-process identity: a host that is also a client can recognize
# itself at handshake time and refuse the self-connection (which would
# otherwise loop every tool/files call back through its own proxy).
INSTANCE_ID = secrets.token_hex(8)

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
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_version": platform.release(),
        "machine": platform.machine(),
        "windows": windows,
        # The host's tools run in this (its Default workspace = home dir).
        "workspace_root": str(Path.home()),
    }


class RemoteSession:
    """Client-side handle on a connected host. Holds only what the
    handshake returned plus the URL + passphrase; nothing is persisted
    backend-side (the frontend remembers hosts and reconnects)."""

    def __init__(self, url: str, passphrase: str, info: dict, app_version: str = ""):
        self.url = url.rstrip("/")
        self.passphrase = passphrase
        self.info = info
        self.app_version = app_version
        self.name = info.get("hostname") or self.url

    @property
    def windows(self) -> bool:
        return bool(self.info.get("windows"))

    def env_line(self) -> str:
        """Runtime-environment sentence for the system prompt: tools run on
        the HOST, so the model must use the host's OS/shell/paths, not the
        client's."""
        i = self.info
        shell = "cmd.exe" if i.get("windows") else "bash/sh"
        return (
            f"Runtime environment: {i.get('os', '?')} {i.get('os_version', '')} "
            f"({i.get('machine', '?')}) on the remote host '{self.name}'. "
            f"The shell tool runs commands there through {shell}; use commands "
            f"and paths valid for THAT operating system. The workspace is the "
            f"host's default workspace ({i.get('workspace_root', 'home')}); "
            "file and shell tools operate there, not on this machine."
        )

    def _headers(self) -> dict:
        return {"X-Yaah-Remote": "1", "X-Yaah-Passphrase": self.passphrase}

    async def exec_tool(self, name: str, args: dict) -> dict:
        """Forward one workspace-tool call to the host. Never raises."""
        if name not in REMOTE_TOOLS:
            return {"error": f"{name} is not a workspace tool; it runs locally"}
        try:
            async with httpx.AsyncClient(timeout=EXEC_TIMEOUT) as client:
                res = await client.post(
                    f"{self.url}/api/remote/exec",
                    json={"name": name, "args": args},
                    headers=self._headers(),
                )
            if res.status_code == 401:
                return {"error": "remote host rejected the passphrase"}
            if res.status_code != 200:
                return {"error": f"remote exec failed ({res.status_code}): {res.text[:200]}"}
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


# One active remote session per backend process (the app drives one host
# at a time). Swapped atomically by the connect/disconnect API.
_active: RemoteSession | None = None


def get_remote() -> RemoteSession | None:
    return _active


def set_remote(session: RemoteSession) -> None:
    global _active
    _active = session


def clear_remote() -> None:
    global _active
    _active = None
