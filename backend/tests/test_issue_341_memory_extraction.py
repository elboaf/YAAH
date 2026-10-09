"""Issues #341/#345: background memory-extraction sub-agent.

After a turn ends, a confined sub-agent reviews the exchange since a
durable cursor and maintains the memory store itself. Tested at three
seams:

- the gates (plan_extraction): run/skip decisions and cursor movement for
  genuine prose / tool-only / main-agent-already-saved / empty windows;
  failures leave the cursor put so content is retried;
- the scheduler (schedule): coalescing by replacement, memory-disabled
  no-op, never raises;
- the writer (run_writer): invoked with the memory-only tool set, runs on
  the sub-agent runner, auto-allowed (no gate), memory_workspace handed
  down;
- the hook (loop): a completed turn schedules extraction with the turn
  workspace; the turn cannot be failed by extraction;
- the charter: the extraction prompt replays the store's own
  when-to-save text by identity.
"""

import asyncio
import json

import pytest

from backend.agent import extract, loop, memory
from backend.agent.tools import MEMORY_TOOLS
from backend.db.database import add_message, create_conversation, init_db


async def _async_blank(workspace):
    return ""


async def _async_none(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Memory ON (extraction inherits that gate), isolated store/state."""
    monkeypatch.setenv("YAAH_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("YAAH_MEMORY_PATH", str(tmp_path / "mem"))
    import importlib

    from backend.agent import config

    saved = dict(vars(config))
    importlib.reload(config)
    config.save_config({"access_mode": "full", "memory": {"enabled": True}})
    extract._reset_state()
    yield tmp_path
    monkeypatch.delenv("YAAH_CONFIG_PATH")
    monkeypatch.delenv("YAAH_MEMORY_PATH")
    for key, value in saved.items():
        setattr(config, key, value)
    extract._reset_state()


@pytest.fixture(autouse=True)
def _db():
    asyncio.run(init_db())


def _window(*rows):
    """Build window rows shaped like get_messages output."""
    out = []
    for i, (role, content) in enumerate(rows, start=11):
        out.append({"id": i, "role": role, "content": content, "meta": None})
    return out


async def _seed(cid, rows):
    """Insert rows; returns their message ids in order."""
    ids = []
    for role, content in rows:
        if isinstance(content, dict):
            ids.append(await add_message(cid, role, content.pop("content"), **content))
        else:
            ids.append(await add_message(cid, role, content))
    return ids


# ---- gates ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_prose_window_plans_a_run_and_keeps_cursor():
    """A genuine user turn plans extraction; the cursor moves only when
    the run completes (asserted in the scheduler tests)."""
    cid = await create_conversation("prose")
    await _seed(
        cid,
        [
            ("user", "use my test server for builds from now on"),
            ("assistant", "Noted, I'll use it."),
        ],
    )
    plan = await extract.plan_extraction(cid)
    assert plan is not None and "window" in plan
    assert [m["role"] for m in plan["window"]] == ["user", "assistant"]
    assert await extract._get_cursor(cid) == 0


@pytest.mark.asyncio
async def test_tool_only_window_skips_and_advances_cursor():
    cid = await create_conversation("tool-only")
    ids = await _seed(
        cid,
        [
            ("assistant", ""),
            ("tool", '{"output": "42"}'),
        ],
    )
    plan = await extract.plan_extraction(cid)
    assert plan == {"skip": "no-user-prose", "boundary": ids[-1]}
    assert await extract._get_cursor(cid) == ids[-1]


@pytest.mark.asyncio
async def test_short_synthetic_prose_skips():
    """Two words is not genuine prose; a meta-tagged agent_prompt row is
    never genuine prose."""
    cid = await create_conversation("synthetic")
    ids = await _seed(cid, [("user", "sounds good")])
    ids += await _seed(
        cid, [("user", {"content": "scheduled prompt", "meta": {"agent_prompt": True}})]
    )
    plan = await extract.plan_extraction(cid)
    assert plan == {"skip": "no-user-prose", "boundary": ids[-1]}


@pytest.mark.asyncio
async def test_direct_memory_write_gate_skips_duplicate_extraction():
    """The main agent already saved memory inside the window: skip (and
    still advance the cursor) so the same fact is never written twice."""
    cid = await create_conversation("already-saved")
    ids = await _seed(
        cid,
        [
            ("user", "remember that I prefer dark UI please and thank you"),
            ("tool", json.dumps({"saved": "prefers-dark-ui", "path": "x"})),
        ],
    )
    plan = await extract.plan_extraction(cid)
    assert plan == {"skip": "direct-memory-write", "boundary": ids[-1]}
    assert await extract._get_cursor(cid) == ids[-1]


@pytest.mark.asyncio
async def test_empty_window_is_none():
    cid = await create_conversation("empty")
    assert await extract.plan_extraction(cid) is None


@pytest.mark.asyncio
async def test_cursor_resume_uses_only_unreviewed_rows():
    cid = await create_conversation("resume")
    await _seed(cid, [("user", "first real prose window here")])
    first = await extract.plan_extraction(cid)
    boundary = first["boundary"]
    await extract._set_cursor(cid, boundary)
    # Nothing new -> None.
    assert await extract.plan_extraction(cid) is None
    # New content since the cursor -> window starts after the boundary.
    await _seed(cid, [("user", "second window with genuine prose")])
    plan = await extract.plan_extraction(cid)
    assert all(m["id"] > boundary for m in plan["window"])


# ---- scheduler ------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_runs_writer_and_advances_cursor(monkeypatch):
    cid = await create_conversation("sched-run")
    await _seed(cid, [("user", "always run builds on my test server")])
    runs = []

    async def fake_run_writer(window, workspace):
        runs.append((list(window), workspace))
        return {"status": "completed"}

    monkeypatch.setattr(extract, "run_writer", fake_run_writer)

    await extract.schedule(cid, "/tmp/ws")
    await asyncio.gather(*extract._tasks)

    assert len(runs) == 1
    assert runs[0][1] == "/tmp/ws"
    assert await extract._get_cursor(cid) == runs[0][0][-1]["id"]


@pytest.mark.asyncio
async def test_failed_run_leaves_cursor_for_retry(monkeypatch):
    """status='error' does not advance the cursor: the window is retried
    with the next turn's content."""
    cid = await create_conversation("sched-fail")
    await _seed(cid, [("user", "one genuine prose window for retry")])

    async def fake_run_writer(window, workspace):
        return {"status": "error", "error": "model down"}

    monkeypatch.setattr(extract, "run_writer", fake_run_writer)
    await extract.schedule(cid, "/tmp/ws")
    await asyncio.gather(*extract._tasks)
    assert await extract._get_cursor(cid) == 0
    # Next schedule (no new rows) replans the SAME window.
    plan = await extract.plan_extraction(cid)
    assert plan is not None and "window" in plan


@pytest.mark.asyncio
async def test_inflight_schedule_replaces_pending_snapshot(monkeypatch):
    """A burst of turn-ends coalesces: the newer snapshot replaces the
    pending one, one run per idle moment, never a queue."""
    cid = await create_conversation("coalesce")
    release = asyncio.Event()

    async def slow_writer(window, workspace):
        await release.wait()
        return {"status": "completed"}

    monkeypatch.setattr(extract, "run_writer", slow_writer)
    ids = await _seed(cid, [("user", "first prose window worth reviewing here")])
    await extract.schedule(cid, "/ws")  # launches the drain (inflight)
    first_boundary = ids[-1]
    ids += await _seed(cid, [("user", "second window arrives while in flight")])
    await extract.schedule(cid, "/ws")  # must REPLACE the pending slot
    pending = extract._pending[cid]
    assert pending["boundary"] == ids[-1] > first_boundary
    release.set()
    await asyncio.gather(*extract._tasks)
    assert extract._pending.get(cid) is None


@pytest.mark.asyncio
async def test_memory_disabled_schedules_nothing(monkeypatch):
    """Extraction inherits memory's opt-in (#169): disabled means no
    writer run and no cursor movement."""
    from backend.agent import config

    config.save_config({"access_mode": "full", "memory": {"enabled": False}})

    ran = []

    async def spy_writer(window, workspace):
        ran.append(True)
        return {"status": "completed"}

    monkeypatch.setattr(extract, "run_writer", spy_writer)
    cid = await create_conversation("disabled")
    await _seed(cid, [("user", "genuine prose that must not be extracted")])
    await extract.schedule(cid, "/ws")
    await asyncio.gather(*extract._tasks)
    assert not ran
    assert await extract._get_cursor(cid) == 0


@pytest.mark.asyncio
async def test_schedule_never_raises(monkeypatch):
    """A broken gate/write path must not take the caller (the turn) down."""

    async def broken(cid):
        raise RuntimeError("db on fire")

    monkeypatch.setattr(extract, "plan_extraction", broken)
    cid = await create_conversation("never-raises")
    await extract.schedule(cid, "/ws")  # must not raise


# ---- writer ---------------------------------------------------------------


def test_writer_catalogue_is_memory_only():
    """The confined writer's resolved tool set is exactly the three memory
    tools - no shell, no network, no files, no agents."""
    from backend.agent.subagents import _resolve_tools

    resolved = _resolve_tools(extract.WRITER_DEF, workspace=".")
    names = {s["function"]["name"] for s in resolved}
    assert names == set(MEMORY_TOOLS)


def test_writer_prompt_carries_manifest_window_and_charter():
    """The prompt embeds the existing-memory manifest, the rendered
    window, and the store's OWN when-to-save charter (identity, not a
    re-typed copy that can drift)."""
    ws = "/tmp/fake-ws"
    prompt = extract._writer_prompt(
        _window(("user", "use the test server for builds"), ("assistant", "Ok.")),
        extract._manifest(ws),
    )
    assert "test server for builds" in prompt
    assert "user:" in prompt
    assert "(none yet)" in prompt
    assert prompt.endswith(memory._WHEN_TO_SAVE)


def test_manifest_lists_existing_memories(tmp_path):
    ws = str(tmp_path / "proj")
    memory.save_memory(
        ws, "uses-test-server", "Uses test server",
        "Builds run on the owner's test server", "user",
        "Builds run on the test server.",
    )
    manifest = extract._manifest(ws)
    assert "uses-test-server" in manifest
    # Headings are stripped: the manifest is pure index lines.
    assert "# Persistent memory" not in manifest


@pytest.mark.asyncio
async def test_run_writer_uses_runner_with_no_gate(monkeypatch):
    """The writer runs through the sub-agent runner, standalone (no
    streaming), with the canonical memory key handed down and the
    access-mode gate OFF (auto-allowed memory maintenance)."""
    seen = {}

    async def fake_run(defn, prompt, workspace, gate=None, **kwargs):
        seen["def"] = defn
        seen["prompt"] = prompt
        seen["workspace"] = workspace
        seen["gate"] = gate
        seen.update(kwargs)
        return {"status": "completed", "output": extract.NOTHING_TO_SAVE}

    monkeypatch.setattr(extract.subagents, "run_sub_agent", fake_run)
    result = await extract.run_writer(
        _window(("user", "remember the test server fact")), "/some/ws"
    )
    assert result["status"] == "completed"
    assert seen["def"] is extract.WRITER_DEF
    assert seen["workspace"] == "/some/ws"
    assert seen["gate"] is None
    assert seen["memory_workspace"] == "/some/ws"


@pytest.mark.asyncio
async def test_run_writer_end_to_end_saves_memory(tmp_path, monkeypatch):
    """Full path with a scripted model: the writer actually saves a
    charter-passing memory via the real memory tools."""
    ws = str(tmp_path / "proj")

    class FakeStream:
        def __init__(self, events):
            self._events = list(events)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._events:
                raise StopAsyncIteration
            return self._events.pop(0)

    def _tc(tool, call_id, **args):
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": tool, "arguments": json.dumps(args)},
        }

    scripts = [
        [
            {
                "type": "tool_calls",
                "tool_calls": [
                    _tc(
                        "memory_save", "c1",
                        name="builds-use-test-server",

                        title="Builds use test server",
                        description="Where the owner runs builds",
                        type="user",
                        content="The owner runs builds on their test server.",
                    )
                ],
            },
        ],
        [{"type": "content", "text": extract.NOTHING_TO_SAVE}, {"type": "finish"}],
    ]

    async def fake_chat(messages, tools=None, stream=True, **kw):
        return FakeStream(scripts.pop(0))

    from backend.agent import model_client

    monkeypatch.setattr(model_client, "chat", fake_chat)
    result = await extract.run_writer(
        _window(("user", "run builds on my test server from now on")), ws
    )
    assert result["status"] == "completed"
    assert (memory.memory_dir(ws) / "builds-use-test-server.md").exists()
    assert "builds-use-test-server" in memory.index_for_prompt(ws)


# ---- loop hook ------------------------------------------------------------


class FakeStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


async def _run_one_turn(monkeypatch, ws, message, title_impl, schedule_impl):
    """Drive one real turn with a scripted model; extraction and title
    generation are replaced at their module seams."""
    scheduled = []

    async def title_or_rec(cid, text):
        if title_impl is not None:
            await title_impl(cid, text)
        scheduled.append(("title", cid, text))

    async def hook(cid, workspace):
        if schedule_impl is not None:
            try:
                await schedule_impl(cid, workspace)
            except Exception as e:  # noqa: BLE001 - record, don't re-raise
                scheduled.append(("extract-error", str(e)))
        scheduled.append(("extract", cid, workspace))

    monkeypatch.setattr(loop, "_generate_conversation_title", title_or_rec)
    monkeypatch.setattr(extract, "schedule", hook)
    monkeypatch.setattr(loop, "_agents_notes_async", _async_blank)
    monkeypatch.setattr(loop, "_memory_notes", lambda w: "")
    monkeypatch.setattr(loop, "_emit_file_changes", _async_none)

    from backend.agent import model_client

    async def fake_chat(messages, tools=None, stream=True, **kw):
        return FakeStream(
            [{"type": "content", "text": "done"}, {"type": "finish"}]
        )

    monkeypatch.setattr(model_client, "chat", fake_chat)

    cid = await create_conversation("hook")
    events = []
    async for e in loop.run_agent(cid, message, ws):
        events.append(json.loads(e))
    # The extraction hook is fire-and-forget: give the detached task its
    # tick before the caller asserts on it.
    await asyncio.gather(*loop._extract_tasks)
    return events, scheduled, cid


@pytest.mark.asyncio
async def test_completed_turn_schedules_extraction(monkeypatch, tmp_path):
    """A natural turn completion schedules extraction once, with the turn
    workspace."""
    ws = str(tmp_path / "proj")
    from pathlib import Path

    Path(ws).mkdir(exist_ok=True)
    events, scheduled, cid = await _run_one_turn(
        monkeypatch, ws, "hello there agent", None, None
    )
    assert any(e["type"] == "done" for e in events)
    assert ("extract", cid, ws) in scheduled
    assert ("title", cid, "hello there agent") in scheduled


@pytest.mark.asyncio
async def test_hook_swallows_scheduler_failure(monkeypatch, tmp_path):
    """A scheduler that raises must not fail the turn (the title seam
    stays healthy - that one is awaited inline and would legitimately
    fail the turn)."""

    async def broken(cid, workspace):
        raise RuntimeError("scheduler exploded")

    ws = str(tmp_path / "proj2")
    from pathlib import Path

    Path(ws).mkdir(exist_ok=True)
    events, _, _ = await _run_one_turn(
        monkeypatch, ws, "another genuine message", None, broken
    )
    assert any(e["type"] == "done" for e in events)
