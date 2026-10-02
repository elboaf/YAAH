"""Issue #188: sub-agent tool schemas resolve through the workspace's own
host (never the legacy active host), and the definition's allowlist is
enforced at execution time too, not only at schema-list time.

SYN-21: `_resolve_tools` used to call get_schemas() with workspace=None,
falling back to the legacy active remote host, while the env line was
grounded per workspace — prompt and schemas could describe two different
hosts. The dead `windows` parameter (client-side os.name) is gone.

SYN-22: explore's read-only promise was enforced only when the schemas
were listed; the executor now rejects any tool outside the resolved set
with a structured error, so a schema leak can't become a write.
"""
import json
from pathlib import Path

import pytest

from backend.agent import subagents
from backend.agent.remote import RemoteSession, register_remote, clear_remote


WIN_INFO = {
    "host_id": "host-win",
    "hostname": "win-host",
    "os": "Windows",
    "windows": True,
    "git_bash": True,
    "workspace_root": "C:/hosts/win",
}
NIX_INFO = {
    "host_id": "host-nix",
    "hostname": "nix-host",
    "os": "Linux",
    "windows": False,
    "workspace_root": "/home/nix",
}

WIN_WS = "remote:host-win:C:/proj"
NIX_WS = "remote:host-nix:/srv/proj"


@pytest.fixture
def two_hosts():
    """A Windows host and a Linux host registered; the LINUX host is the
    legacy active one — exactly the split-brain the issue describes."""
    register_remote(RemoteSession("http://win", "p", dict(WIN_INFO)))
    register_remote(RemoteSession("http://nix", "p", dict(NIX_INFO)),
                    make_active=True)
    yield
    clear_remote()


def _names(defn, workspace):
    return {
        s["function"]["name"]
        for s in subagents._resolve_tools(defn, workspace=workspace)
    }


def test_schemas_follow_the_workspace_host(two_hosts):
    """SYN-21: schemas resolve from the workspace's owning host, not the
    legacy active host and not the client's os.name."""
    gp = subagents.get_agent_def("general-purpose")
    # Workspace on the Windows host: powershell is offered even though
    # the legacy ACTIVE host is Linux.
    assert "powershell" in _names(gp, WIN_WS)
    # Workspace on the Linux host: no powershell, even on a Windows
    # client (the old code passed windows=os.name == "nt" here).
    assert "powershell" not in _names(gp, NIX_WS)


def test_schemas_local_workspace_uses_local_host(two_hosts):
    """A non-namespaced workspace keeps the legacy behavior: the active
    host (Linux here) shapes the schema set — no powershell."""
    gp = subagents.get_agent_def("general-purpose")
    assert "powershell" not in _names(gp, "C:/local/ws")


def test_env_line_and_schemas_describe_the_same_host(two_hosts):
    """SYN-21 second half: the env line is grounded by the SAME host
    resolution as the schemas — workspace on the Windows host says so,
    regardless of which host happens to be the legacy active one."""
    defn = subagents.get_agent_def("general-purpose")
    prompt = subagents._sub_agent_system_prompt(defn, WIN_WS)
    assert "Git Bash" in prompt          # Windows host's shell, not bash/sh
    assert "C:/proj on the host" in prompt
    # And the tools sentence derives from the same resolution: powershell
    # (offered by the win host) is named.
    assert "powershell" in prompt


def test_windows_dead_parameter_is_gone():
    """The caller-side `windows` flag (client os.name) no longer exists:
    host shape comes from the workspace's host resolution."""
    import inspect

    params = inspect.signature(subagents._resolve_tools).parameters
    assert "windows" not in params


@pytest.mark.asyncio
async def test_allowlist_enforced_at_execution_time(monkeypatch, tmp_path):
    """SYN-22: even if a write schema leaked through, the executor
    refuses tools outside the agent's resolved set with a structured
    error — the allowlist is not only a schema-list filter."""
    streams: list[list[dict]] = [
        [
            {"type": "tool_calls", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps(
                        {"path": "victim.txt", "content": "pwn"}
                    ),
                },
            }]},
            {"type": "finish", "reason": "tool_calls"},
        ],
        [
            {"type": "content", "text": "stopped at the gate"},
            {"type": "finish"},
        ],
    ]

    class _Stream:
        def __init__(self, events):
            self._events = list(events)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._events:
                raise StopAsyncIteration
            return self._events.pop(0)

    async def fake_chat(messages, tools=None, stream=True):
        return _Stream(streams.pop(0))

    monkeypatch.setattr(subagents.model_client, "chat", fake_chat)

    result = await subagents.run_sub_agent(
        subagents.get_agent_def("explore"), "write a file", str(tmp_path)
    )
    assert result["status"] == "completed"
    assert result["output"] == "stopped at the gate"
    tool_entry = result["transcript"][2]
    assert tool_entry["name"] == "write_file"
    assert "error" in json.loads(tool_entry["content"])
    assert not (tmp_path / "victim.txt").exists()
