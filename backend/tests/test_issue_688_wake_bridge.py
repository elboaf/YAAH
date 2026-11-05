"""Wake bridge (issue #688) — tests follow the issue's acceptance list:
signal vs noise (timeout never fires), busy target queues instead of a
second stream, burst coalescing with a correct count, partial-line and
truncation tolerance, disabled-block = zero behavior change, and the
Settings-API partial merge. The parsing half is driven directly (no
clock); the firing half via a patched _fire_wake, so no test runs a real
model turn."""
import asyncio
import json
import time
import uuid

import pytest

from backend.agent import loop, wake
from backend.db.database import create_conversation


def make_watcher(tmp_path, lines=(), debounce=0.0, **kw):
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
    return wake.WakeFileWatcher(str(path), kw.pop("conversation_id", 42),
                                debounce_seconds=debounce, **kw), path


def append(path, *events):
    with open(path, "a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


@pytest.fixture
def fire_spy(monkeypatch):
    """Watcher-level tests record fires instead of starting real turns."""
    seen = []

    async def spy(conv_id, prompt):
        seen.append((conv_id, prompt))
        return "started"

    monkeypatch.setattr(wake, "_fire_wake", spy)
    return seen


@pytest.fixture(autouse=True)
def _clean_globals():
    wake.stop_wake_watcher()
    yield
    wake.stop_wake_watcher()


# ------------------------------------------------------------ line parsing

def test_actionable_types_fire_and_heartbeat_does_not(tmp_path):
    w, _ = make_watcher(tmp_path)
    assert w.handle_line(json.dumps({"type": "steer"})) == "steer"
    assert w.handle_line(json.dumps({"type": "generate"})) == "generate"
    assert w.handle_line(json.dumps({"type": "accept"})) == "accept"
    assert w.handle_line(json.dumps({"type": "exit"})) == "exit"  # unknown types wake too
    assert w.handle_line(json.dumps({"type": "timeout"})) is None  # the only heartbeat


def test_garbage_lines_never_wake(tmp_path):
    w, _ = make_watcher(tmp_path)
    assert w.handle_line("not json at all") is None
    assert w.handle_line('["a", "list"]') is None
    assert w.handle_line("") is None
    assert w.handle_line('{"no": "type"}') is None


def test_types_override_replaces_default_set(tmp_path):
    w, _ = make_watcher(tmp_path, types=["timeout"])
    assert w.handle_line(json.dumps({"type": "timeout"})) == "timeout"
    assert w.handle_line(json.dumps({"type": "steer"})) is None


def test_tail_completes_partial_lines_and_counts_bytes(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"type": "steer"}\n{"type": "gen', encoding="utf-8")
    lines, off = wake.tail_new_lines(str(path), 0)
    assert lines == ['{"type": "steer"}']  # partial line held back
    lines, off = wake.tail_new_lines(str(path), off)
    assert lines == []  # still nothing complete
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('erate"}\n')
    lines, off = wake.tail_new_lines(str(path), off)
    assert lines == ['{"type": "generate"}']
    assert off == path.stat().st_size


def test_tail_survives_missing_file(tmp_path):
    lines, off = wake.tail_new_lines(str(tmp_path / "nope.jsonl"), 0)
    assert lines == [] and off == 0


def test_tail_survives_multibyte_content(tmp_path):
    path = tmp_path / "events.jsonl"
    payload = json.dumps(
        {"type": "steer", "message": "héllo ✓ wörld"}, ensure_ascii=False
    )
    path.write_text(payload + "\n", encoding="utf-8")
    lines, off = wake.tail_new_lines(str(path), 0)
    assert lines == [payload]
    assert off == path.stat().st_size  # byte-accurate, not char-accurate


def test_tail_does_not_split_on_unicode_line_separators(tmp_path):
    """U+2028 inside a JSON string is legal and emitted raw by
    JSON.stringify / json.dumps(ensure_ascii=False); splitlines() would
    cut the event in half (both fragments fail json.loads — silent
    drop). A '\n'-only split keeps it whole."""
    path = tmp_path / "events.jsonl"
    payload = json.dumps(
        {"type": "steer", "message": "line\u2028sep\u2029end\u0085"}, ensure_ascii=False
    )
    assert "\n" not in payload  # the separators survived json.dumps raw
    path.write_text(payload + "\n", encoding="utf-8")
    lines, off = wake.tail_new_lines(str(path), 0)
    assert lines == [payload]
    assert off == path.stat().st_size


# ------------------------------------------------------------ config block

def test_wake_config_defaults_and_sanitizing():
    block = wake.read_wake_config({})
    assert block == {
        "enabled": False, "path": "", "conversation_id": 0,
        "debounce_seconds": wake.DEFAULT_DEBOUNCE_SECONDS, "types": None,
        "prompt": "",
    }
    block = wake.read_wake_config({"wake": {
        "enabled": True, "path": " C:/j/events.jsonl ",
        "conversation_id": "7", "debounce_seconds": "junk",
        "types": "not-a-list",
    }})
    assert block["enabled"] is True
    assert block["path"] == "C:/j/events.jsonl"
    assert block["conversation_id"] == 7
    assert block["debounce_seconds"] == wake.DEFAULT_DEBOUNCE_SECONDS
    assert block["types"] is None  # invalid override falls back to defaults


def test_spec_file_key_is_accepted_as_path_alias():
    """The issue text says `file`; the shipped key is `path` — both work."""
    block = wake.read_wake_config(
        {"wake": {"file": "C:/j/events.jsonl", "conversation_id": 3}}
    )
    assert block["path"] == "C:/j/events.jsonl"


def test_custom_prompt_is_used_verbatim():
    w = wake.WakeFileWatcher("C:/j/events.jsonl", 1, prompt="my own words")
    assert w.prompt_for(99) == "my own words"
    default = wake.WakeFileWatcher("C:/j/events.jsonl", 1)
    assert "99 new event" in default.prompt_for(99)


def test_wake_config_debounce_clamped():
    assert wake.read_wake_config(
        {"wake": {"debounce_seconds": 999}}
    )["debounce_seconds"] == wake.DEBOUNCE_MAX
    assert wake.read_wake_config(
        {"wake": {"debounce_seconds": -5}}
    )["debounce_seconds"] == wake.DEBOUNCE_MIN


# ------------------------------------------------------------ watcher loop

async def test_boot_starts_at_file_end_no_catchup(tmp_path, fire_spy):
    w, _ = make_watcher(tmp_path, lines=[{"type": "steer"}, {"type": "generate"}])
    await w.poll_once()  # pre-existing events predate the watcher
    assert w._pending_count == 0
    assert fire_spy == []


async def test_actionable_event_fires_after_debounce(tmp_path, fire_spy):
    w, path = make_watcher(tmp_path)  # debounce 0: fires on the same poll
    append(path, {"type": "steer", "message": "hi"})
    await w.poll_once()
    assert len(fire_spy) == 1
    conv_id, prompt = fire_spy[0]
    assert conv_id == 42
    assert "1 new event" in prompt and str(path) in prompt


async def test_burst_coalesces_into_one_wake_with_count(tmp_path, fire_spy):
    w, path = make_watcher(tmp_path, debounce=30)
    append(path, {"type": "steer"}, {"type": "generate"}, {"type": "accept"})
    await w.poll_once()
    await w.poll_once()
    await w.poll_once()
    assert fire_spy == []  # quiet window not yet passed
    w._last_action_at = time.monotonic() - 31  # window passes
    await w.poll_once()
    assert len(fire_spy) == 1
    assert "3 new event" in fire_spy[0][1]
    # A second poll does not re-fire the consumed burst.
    await w.poll_once()
    assert len(fire_spy) == 1


async def test_heartbeats_never_fire_alone(tmp_path, fire_spy):
    w, path = make_watcher(tmp_path)
    append(path, {"type": "timeout"}, {"type": "timeout"})
    await w.poll_once()
    assert fire_spy == [] and w._pending_count == 0


async def test_heartbeats_between_events_do_not_inflate_count(tmp_path, fire_spy):
    w, path = make_watcher(tmp_path)
    append(path, {"type": "timeout"}, {"type": "steer"}, {"type": "timeout"})
    await w.poll_once()
    assert len(fire_spy) == 1
    assert "1 new event" in fire_spy[0][1]


async def test_each_burst_fires_exactly_once(tmp_path, fire_spy):
    w, path = make_watcher(tmp_path)
    append(path, {"type": "steer"})
    await w.poll_once()
    append(path, {"type": "generate"})
    await w.poll_once()
    assert len(fire_spy) == 2  # two separate wakes, no echo loop
    assert "1 new event" in fire_spy[1][1]


async def test_truncation_restarts_from_zero(tmp_path, fire_spy):
    w, path = make_watcher(tmp_path, debounce=60)  # absorb without firing
    append(path, {"type": "steer"})
    await w.poll_once()
    assert w._pending_count == 1
    path.write_text(json.dumps({"type": "generate"}) + "\n", encoding="utf-8")
    await w.poll_once()  # journal rotated: the old burst is gone
    assert w._pending_count == 1  # only the new generation's line counts


async def test_equal_size_replacement_restarts_from_zero(tmp_path, fire_spy):
    """Deleted and recreated at the SAME byte size — CodeRabbit's rotation
    case: size comparison alone can never see it, the file id does. (An
    in-place same-size overwrite keeps the file id and is indistinguishable
    from no change at the offset level; real rotation replaces the file.)"""
    w, path = make_watcher(tmp_path, debounce=60)
    append(path, {"type": "discard"})  # same length as "timeout"
    await w.poll_once()
    assert w._pending_count == 1
    path.unlink()
    path.write_text(json.dumps({"type": "timeout"}) + "\n", encoding="utf-8")
    assert path.stat().st_size == w._size  # precondition: equal size
    await w.poll_once()  # replacement seen via file id: stale burst dropped
    assert w._pending_count == 0  # timeout is noise AND the old burst is gone
    append(path, {"type": "accept"})
    await w.poll_once()
    assert w._pending_count == 1  # offset reset: the accept line was read


async def test_disappear_then_recreate_smaller_resumes(tmp_path, fire_spy):
    """Deleted at one poll, recreated smaller at the next: without the
    identity reset the offset seeks past EOF and the watcher goes silent
    until the new file outgrows the old offset (CodeRabbit's case)."""
    w, path = make_watcher(tmp_path, debounce=60)
    append(path, {"type": "steer"}, {"type": "generate"})
    await w.poll_once()
    assert w._pending_count == 2
    path.unlink()
    await w.poll_once()  # missing: identity forgotten
    assert w._file_id is None and w._size == 0
    path.write_text(json.dumps({"type": "accept"}) + "\n", encoding="utf-8")
    await w.poll_once()
    assert w._pending_count == 1  # the new journal's line, fresh from zero


async def test_missing_file_is_silently_tolerated(tmp_path, fire_spy):
    path = tmp_path / "events.jsonl"
    w = wake.WakeFileWatcher(str(path), 42)  # never created
    await w.poll_once()
    assert fire_spy == []


# ------------------------------------------------------------ firing paths

async def test_busy_target_queues_instead_of_second_stream(monkeypatch):
    queued = []
    monkeypatch.setattr(loop, "agent_is_running", lambda cid: True)

    def fake_enqueue(cid, text):
        queued.append((cid, text))
        return {"id": 1}

    monkeypatch.setattr(loop, "enqueue_message", fake_enqueue)
    result = await wake._fire_wake(7, "the prompt")
    assert result == "queued"
    assert queued == [(7, "the prompt")]


async def test_idle_busy_race_recovers_through_the_queue(monkeypatch):
    """The target starts a turn between our busy-check and the claim:
    run_agent refuses with kind=busy, and the wake must land in the
    queue — not vanish (the issue's 'no 409, no lost event' intent)."""
    queued = []

    async def refuses_as_busy(conv_id, prompt, workspace, **kw):
        yield '{"type": "error", "fatal": false, "kind": "busy"}'

    monkeypatch.setattr(loop, "run_agent", refuses_as_busy)
    monkeypatch.setattr(
        loop, "enqueue_message",
        lambda cid, text: queued.append((cid, text)) or {"id": 1},
    )
    conv_id = await create_conversation("race target", workspace="C:/ws")
    result = await wake._fire_wake(conv_id, "the prompt")
    assert result == "started"
    await asyncio.sleep(0.05)  # let the drain task hit the refusal
    assert queued == [(conv_id, "the prompt")]


async def test_idle_target_starts_turn_with_chat_scope(monkeypatch):
    conv_id = await create_conversation(
        "wake target", workspace="C:/some/ws",
        model="prov::m1", effort="high", subagent_model="prov::sub1",
    )
    calls = []

    async def fake_run_agent(conv_id, prompt, workspace, **kw):
        calls.append((conv_id, prompt, workspace, kw))
        yield '{"type": "done"}'

    monkeypatch.setattr(loop, "run_agent", fake_run_agent)
    import backend.main as main_mod

    scope_args = []

    def fake_scope(conv, agent=None):
        scope_args.append(conv)  # capture: the resolver must get OUR row
        return ("prov::m1", "high", "prov::sub1")

    monkeypatch.setattr(main_mod, "_resolve_turn_scope", fake_scope)
    result = await wake._fire_wake(conv_id, "the prompt")
    assert result == "started"
    await asyncio.sleep(0.05)  # let the drain task consume the stream
    assert calls, "run_agent never consumed"
    got_conv, prompt, workspace, kw = calls[0]
    assert got_conv == conv_id
    assert scope_args and scope_args[0]["id"] == conv_id
    assert scope_args[0]["workspace"] == "C:/some/ws"
    assert workspace == "C:/some/ws"
    assert kw["model_override"] == "prov::m1"
    assert kw["effort_override"] == "high"
    assert kw["subagent_scope"] == "prov::sub1"


async def test_gone_target_is_logged_not_fired():
    assert await wake._fire_wake(999999, "prompt") == "gone"


async def test_watcher_end_to_end_busy_path(tmp_path, monkeypatch):
    """Watcher absorbs an event while the chat runs -> the queue gets the
    wake prompt (the issue's mid-run acceptance), no new stream starts."""
    w, path = make_watcher(tmp_path, conversation_id=5)
    queued = []
    monkeypatch.setattr(loop, "agent_is_running", lambda cid: True)
    monkeypatch.setattr(
        loop, "enqueue_message",
        lambda cid, text: queued.append((cid, text)) or {"id": 1},
    )
    append(path, {"type": "generate"})
    await w.poll_once()
    assert queued == [(5, w.prompt_for(1))]


# ------------------------------------------------------------ start gate

async def test_disabled_block_starts_nothing(monkeypatch):
    original = wake.read_wake_config
    monkeypatch.setattr(
        wake, "read_wake_config",
        lambda cfg=None: dict(original({}), enabled=False),
    )
    assert wake.start_wake_watcher() is False
    assert wake._task is None


async def test_incomplete_block_starts_nothing(monkeypatch):
    monkeypatch.setattr(wake, "read_wake_config", lambda cfg=None: {
        "enabled": True, "path": "", "conversation_id": 0,
        "debounce_seconds": 3.0, "types": None, "prompt": "",
    })
    assert wake.start_wake_watcher() is False  # half-typed config must not fire at conv 0
    assert wake._task is None


async def test_start_stop_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setattr(wake, "read_wake_config", lambda cfg=None: {
        "enabled": True, "path": str(tmp_path / "events.jsonl"),
        "conversation_id": 42, "debounce_seconds": 3.0, "types": None,
        "prompt": "",
    })
    assert wake.start_wake_watcher() is True
    assert wake._task is not None and not wake._task.done()
    assert wake._watcher is not None
    # A second start is idempotent.
    assert wake.start_wake_watcher() is True
    wake.stop_wake_watcher()
    assert wake._task is None and wake._watcher is None


# ------------------------------------------------------------ settings API

def test_settings_roundtrip_and_partial_merge():
    """The wake block rides the Settings API like voice/remote: a save
    that only touches enabled must not wipe siblings, and an unrelated
    save must not wipe wake."""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        r = client.put("/api/config", json={"wake": {"enabled": True}})
        assert r.status_code == 200, r.text
        view = client.get("/api/config").json()
        assert view["wake"]["enabled"] is True
        assert view["wake"]["debounce_seconds"] == wake.DEFAULT_DEBOUNCE_SECONDS
        # An unrelated Settings save leaves the wake block alone.
        r = client.put("/api/config", json={"max_steps": 25})
        assert r.status_code == 200
        view = client.get("/api/config").json()
        assert view["wake"]["enabled"] is True
        # A partial wake save merges over the stored block.
        r = client.put(
            "/api/config",
            json={"wake": {"conversation_id": 3, "path": "C:/j/x.jsonl",
                           "prompt": "custom words"}},
        )
        assert r.status_code == 200
        view = client.get("/api/config").json()
        assert view["wake"]["enabled"] is True  # survived the partial save
        assert view["wake"]["conversation_id"] == 3
        assert view["wake"]["prompt"] == "custom words"
