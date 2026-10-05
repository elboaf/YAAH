"""Tool context injection on every dispatch path (issue #303).

`conversation_id` is injected per-tool where the context is only known
to the harness (search_conversation_history scopes history; branch_select
stores the chat's own selector, #277). The injection used to live at a
single call site — the main loop's direct dispatch — so two paths
silently ran those tools WITHOUT the chat id:

1. the ask-mode gate's approved re-execution, and
2. every sub-agent tool execution (general-purpose sub-agents can call
   branch_select, and the slice-C note tells them the SOP).

Both turn shapes below go red before the fix and green after.
"""
import asyncio
import json
import subprocess

import pytest

from backend.agent import loop
from backend.db.database import create_conversation, get_conversation


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
    """Patch model_client.chat to return scripted responses per call.

    Sub-agents share this module-level client, so parent and sub turns
    both consume the same script queue, in fire order."""
    scripts: list[list[dict]] = []

    async def fake_chat(messages, tools=None, stream=True):
        events = scripts.pop(0) if scripts else [{"type": "finish"}]
        return FakeStream(events)

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    return scripts


async def collect(agent_gen):
    out = []
    async for e in agent_gen:
        out.append(json.loads(e))
    return out


def _tool_call(name, call_id="c1", **args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


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


def _tool_result(events):
    return next(e for e in events if e["type"] == "tool_result")


# ---- 1. the gate path: approved branch_select keeps its chat context ----


@pytest.mark.asyncio
async def test_gate_approved_branch_select_stores_pin(fake_model, tmp_path, monkeypatch):
    repo = _repo_with_branch(tmp_path)
    cid = await create_conversation("gate flip")
    # The installed app's posture (its config carries no access_mode):
    # ask — mutating tools hit the approval gate.
    monkeypatch.setattr(loop, "current_access_mode", lambda: "ask")
    fake_model.append([
        {"type": "tool_calls", "tool_calls": [_tool_call("branch_select", branch="feature")]},
    ])
    fake_model.append([{"type": "content", "text": "flipped"}, {"type": "finish"}])

    gen = loop.run_agent(cid, "switch me to feature", str(repo))
    task = asyncio.create_task(collect(gen))

    async def approver():
        for _ in range(3000):
            await asyncio.sleep(0.01)
            if task.done():
                return
            if loop.resolve_answer(cid, "c1", "approve"):
                return
        pytest.fail("approval request never surfaced")

    await asyncio.gather(task, approver())
    events = task.result()

    tr = _tool_result(events)
    assert tr["name"] == "branch_select"
    assert tr["result"].get("ok") is True, tr["result"]
    conv = await get_conversation(cid)
    assert (conv["selected_branch"], conv["branch_pin_origin"]) == ("feature", "explicit"), (
        "the approved branch_select ran without conversation context "
        "(#303): the pin was never stored"
    )


# ---- 2. the sub-agent path: a sub's branch_select stores the parent pin ----


@pytest.mark.asyncio
async def test_sub_agent_branch_select_stores_parent_pin(fake_model, tmp_path):
    repo = _repo_with_branch(tmp_path)
    cid = await create_conversation("sub flip")
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "s1",
                "type": "function",
                "function": {
                    "name": "spawn_agent",
                    "arguments": json.dumps({
                        "agent_type": "general-purpose",
                        "prompt": "Point this chat's selector at feature: call branch_select with branch=feature.",
                    }),
                },
            }],
        },
    ])
    # The sub-agent's single model step (same client, same queue).
    fake_model.append([
        {"type": "tool_calls", "tool_calls": [_tool_call("branch_select", call_id="sub-c1", branch="feature")]},
        {"type": "finish"},
    ])
    fake_model.append([{"type": "content", "text": "delegated"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "delegate the flip", str(repo)))

    conv = await get_conversation(cid)
    assert (conv["selected_branch"], conv["branch_pin_origin"]) == ("feature", "explicit"), (
        "the sub-agent's branch_select ran without conversation context "
        "(#303): the parent chat's pin was never stored"
    )
