"""Issue #346: one canonical memory key per workspace.

The prompt's injected index keys memory off the run's ORIGINAL workspace
argument, while mid-turn memory tool calls resolved from the POST-rebind
chat-worktree path (`<workspace>/.scratch/chat-<id>/`) - so in a
branch-pinned chat a memory_save landed in a per-chat store the prompt
block never read.

The fix resolves the workspace root ONCE per turn and injects it into
memory tool calls at the single funnel every dispatch path shares
(`execute_tool` - direct calls, gate-approved re-execution, and
sub-agents all route through it, the #303 injection precedent), so
every path agrees on the canonical key: the logical workspace root.

Shapes under test:
1. a branch-pinned chat turn whose memory_save must land in the same
   store the prompt's injected index reads;
2. the sub-agent dispatch path (spawn inside the pinned turn);
3. non-pinned chats / non-git workspaces unchanged (same store as the
   primary - the injection is a no-op when no rebind happened);
4. remote namespacing preserved (`remote:<host>:<path>` still hashes
   the raw namespaced string - client-local memories by design).
"""
import asyncio
import json
import subprocess

import pytest

from backend.agent import loop, memory
from backend.db.database import create_conversation


class FakeStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


@pytest.fixture
def fake_model(monkeypatch):
    """Script model_client.chat responses per call, in fire order."""
    scripts: list[list[dict]] = []

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        events = scripts.pop(0) if scripts else [{"type": "finish"}]
        return FakeStream(events)

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    return scripts


async def collect(agent_gen):
    out = []
    async for e in agent_gen:
        out.append(json.loads(e))
    return out


def _tool_call(tool_name, call_id="c1", **args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": tool_name, "arguments": json.dumps(args)},
    }


def _tool_result(events):
    return next(e for e in events if e["type"] == "tool_result")


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture()
def mem_cfg(tmp_path, monkeypatch):
    """Memory ON (opt-in since #169), isolated config + memory root."""
    monkeypatch.setenv("YAAH_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("YAAH_MEMORY_PATH", str(tmp_path / "mem"))
    import importlib

    from backend.agent import config

    saved = dict(vars(config))
    importlib.reload(config)
    config.save_config({"access_mode": "full", "memory": {"enabled": True}})
    yield tmp_path
    monkeypatch.delenv("YAAH_CONFIG_PATH")
    monkeypatch.delenv("YAAH_MEMORY_PATH")
    for key, value in saved.items():
        setattr(config, key, value)


def _repo_with_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    _git(repo, "branch", "feature")
    return repo


def _pinned_store(tmp_path, ws):
    """The store the pre-rebind workspace resolves to."""
    return memory.memory_dir(str(ws))


# ---- 1. the pinned-turn shape: save and injected index agree -------------


@pytest.mark.asyncio
async def test_save_in_pinned_chat_lands_in_prompt_store(
    fake_model, mem_cfg, tmp_path, monkeypatch
):
    """A branch-pinned chat's memory_save must write the store the turn's
    injected index was read from (the workspace root). Under the direct
    world (#361) no rebind exists at all - the funnel injection is the
    guarantee that saves resolve the prompt's store."""
    repo = _repo_with_branch(tmp_path)
    monkeypatch.setattr(loop, "_agents_notes", lambda ws: "")
    monkeypatch.setattr(loop, "_agents_notes_async", _async_blank)
    monkeypatch.setattr(loop, "_memory_notes", lambda ws: "")
    cid = await create_conversation("pinned save")

    fake_model.append([
        {"type": "tool_calls", "tool_calls": [
            _tool_call(
                "memory_save",
                name="prefers-dark-ui", title="Prefers dark UI",
                description="User prefers dark themes", type="user",
                content="The user prefers dark UI themes.",
            )
        ]},
    ])
    fake_model.append([{"type": "content", "text": "saved"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "remember this", str(repo)))

    result = _tool_result(events)["result"]
    assert "error" not in result, result
    saved_dir = memory.memory_dir(str(repo))
    assert (saved_dir / "prefers-dark-ui.md").exists(), (
        "memory_save in a branch-pinned chat wrote a per-chat store; "
        "it must resolve to the canonical workspace root (#346)"
    )
    # The prompt's injected index reads exactly the canonical store.
    assert "prefers-dark-ui" in memory.index_for_prompt(str(repo))


@pytest.mark.asyncio
async def test_pinned_read_and_prompt_index_agree(
    fake_model, mem_cfg, tmp_path, monkeypatch
):
    """A memory saved by ANOTHER chat of the same workspace surfaces in
    this pinned chat's injected index AND is readable by its tool calls
    - one store per workspace, not per chat tree."""

    repo = _repo_with_branch(tmp_path)
    monkeypatch.setattr(loop, "_agents_notes", lambda ws: "")
    monkeypatch.setattr(loop, "_agents_notes_async", _async_blank)
    monkeypatch.setattr(loop, "_memory_notes", lambda ws: "")
    cid = await create_conversation("pinned read")

    memory.save_memory(
        str(repo), "deploys-via-blue-green", "Deploys via blue-green",
        "Release pipeline fact", "project",
        "This project deploys blue-green.",
    )

    fake_model.append([
        {"type": "tool_calls", "tool_calls": [
            _tool_call("memory_read", name="deploys-via-blue-green")
        ]},
    ])
    fake_model.append([{"type": "content", "text": "ok"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "read the deploy fact", str(repo)))
    r = _tool_result(events)["result"]
    assert "error" not in r, r
    assert "blue-green" in r.get("content", "")


# ---- 2. the sub-agent dispatch path --------------------------------------


@pytest.mark.asyncio
async def test_sub_agent_save_in_pinned_chat_uses_prompt_store(
    fake_model, mem_cfg, tmp_path, monkeypatch
):
    """Mid-turn sub-agents receive the turn's workspace resolution too.
    The funnel injection must cover them: a sub-agent's save lands in
    the prompt's store."""

    repo = _repo_with_branch(tmp_path)
    monkeypatch.setattr(loop, "_agents_notes", lambda ws: "")
    monkeypatch.setattr(loop, "_agents_notes_async", _async_blank)
    monkeypatch.setattr(loop, "_memory_notes", lambda ws: "")
    cid = await create_conversation("sub save")

    fake_model.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "s1",
            "type": "function",
            "function": {
                "name": "spawn_agent",
                "arguments": json.dumps({
                    "agent_type": "general-purpose",
                    "prompt": "Save a memory: call memory_save with "
                              "name=uses-make, title=Uses make, "
                              "description=Build system, type=project, "
                              "content=Builds with make.",
                }),
            },
        }]},
    ])
    fake_model.append([
        {"type": "tool_calls", "tool_calls": [
            _tool_call(
                "memory_save", call_id="sub-c1",
                name="uses-make", title="Uses make",
                description="Build system", type="project",
                content="Builds with make.",
            )
        ]},
        {"type": "finish"},
    ])
    fake_model.append([{"type": "content", "text": "delegated"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "delegate the save", str(repo)))

    spawn_result = next(
        e for e in events
        if e["type"] == "tool_result" and e.get("name") == "spawn_agent"
    )
    assert "error" not in (spawn_result["result"] or {}), spawn_result["result"]
    saved_dir = memory.memory_dir(str(repo))
    assert (saved_dir / "uses-make.md").exists(), (
        "the sub-agent's memory_save wrote a per-chat store; the funnel "
        "injection must cover the sub-agent dispatch path (#346)"
    )


# ---- 3. non-pinned chats behave exactly as before ------------------------


@pytest.mark.asyncio
async def test_non_pinned_chat_unchanged(fake_model, mem_cfg, tmp_path, monkeypatch):
    """No branch pin anywhere - the injection is a no-op: same store,
    same behavior as before the fix."""
    repo = _repo_with_branch(tmp_path)
    monkeypatch.setattr(loop, "_agents_notes", lambda ws: "")
    monkeypatch.setattr(loop, "_agents_notes_async", _async_blank)
    monkeypatch.setattr(loop, "_memory_notes", lambda ws: "")
    cid = await create_conversation("plain save")

    fake_model.append([
        {"type": "tool_calls", "tool_calls": [
            _tool_call(
                "memory_save",
                name="plain-fact", title="Plain fact",
                description="d", type="project", content="plain",
            )
        ]},
    ])
    fake_model.append([{"type": "content", "text": "ok"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "save", str(repo)))
    r = _tool_result(events)["result"]
    assert "error" not in r, r
    assert (memory.memory_dir(str(repo)) / "plain-fact.md").exists()


@pytest.mark.asyncio
async def test_non_git_workspace_unchanged(fake_model, mem_cfg, tmp_path, monkeypatch):
    """A NON-git workspace keeps memory resolving to the workspace
    itself - no store migration, no error."""
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(loop, "_agents_notes", lambda ws: "")
    monkeypatch.setattr(loop, "_agents_notes_async", _async_blank)
    monkeypatch.setattr(loop, "_memory_notes", lambda ws: "")
    cid = await create_conversation("non-git pin")

    fake_model.append([
        {"type": "tool_calls", "tool_calls": [
            _tool_call(
                "memory_save",
                name="plain-git-fact", title="P", description="d",
                type="project", content="x",
            )
        ]},
    ])
    fake_model.append([{"type": "content", "text": "ok"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "save", str(plain)))
    r = _tool_result(events)["result"]
    assert "error" not in r, r
    assert (memory.memory_dir(str(plain)) / "plain-git-fact.md").exists()


# ---- 4. remote namespacing preserved -------------------------------------


def test_remote_namespacing_not_collapsed(mem_cfg, tmp_path):
    """The canonical key is the LOGICAL workspace string: remote
    namespaces still hash their raw `remote:<host>:<path>` form so a
    remote project's memories stay client-local (#346 constraint) - and
    differently from ANY local path of the same spelling."""
    ns = "remote:h1:C:\\proj"
    local = "C:\\proj"
    assert memory.project_key(ns) != memory.project_key(local)
    assert memory.memory_dir(ns) != memory.memory_dir(local)
    # ...and the injection resolves the SAME string for a remote workspace:
    from backend.agent.tools import memory_workspace_for

    assert memory_workspace_for(ns) == ns


# ---- unit: the injection itself ------------------------------------------


@pytest.mark.asyncio
async def test_execute_tool_injects_memory_workspace(mem_cfg, monkeypatch, tmp_path):
    """Direct funnel test: memory tools execute against the injected
    memory_workspace, other tools keep their own workspace argument."""
    from backend.agent.tools import execute_tool

    chat_tree = tmp_path / "repo" / ".scratch" / "chat-9"
    chat_tree.mkdir(parents=True)
    canonical = tmp_path / "repo"

    r = await execute_tool(
        "memory_save",
        {"name": "inject-check", "title": "I", "description": "d",
         "type": "project", "content": "c"},
        str(chat_tree),
        memory_workspace=str(canonical),
    )
    assert "error" not in r, r
    assert (memory.memory_dir(str(canonical)) / "inject-check.md").exists()
    # and NOT in the per-chat store the call workspace would have hashed:
    assert not (memory.memory_dir(str(chat_tree)) / "inject-check.md").exists()


@pytest.mark.asyncio
async def test_execute_tool_without_injection_unchanged(mem_cfg, tmp_path):
    """No injected root (direct callers, tests): behavior identical to
    before - the call workspace resolves the store, as always."""
    from backend.agent.tools import execute_tool

    ws = tmp_path / "solo"
    ws.mkdir()
    r = await execute_tool(
        "memory_read", {"name": "nope"}, str(ws),
    )
    assert r == {"error": "No memory named nope"}


async def _async_blank(workspace):
    return ""
