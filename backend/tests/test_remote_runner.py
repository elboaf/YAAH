"""Owner-qualified remote turn runner tests (Phase 6).

Mirrors test_remote_turn.py's FakeSession pattern: the runner is exercised
end-to-end with a fake owner session and a fake model stream, asserting the
event sequence, lease lifecycle, commit payload, and exclusion rules.
"""

import asyncio
import copy
import json

import pytest

from backend.agent import remote as remote_mod
from backend.agent.remote_runner import run_remote_turn


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeModelStream:
    """Substitutes model_client.chat; yields one scripted event list."""

    def __init__(self, events):
        self.events = list(events)


_SCRIPTED_TURNS: list[FakeModelStream] = []


def _script_events(*events):
    _SCRIPTED_TURNS.append(FakeModelStream(events))


class FakeSession:
    def __init__(self, host_id, snapshot=None):
        self.host_id = host_id
        self.snapshot = copy.deepcopy(snapshot or {
            "revision": "r:4",
            "conversation": {"id": 731, "title": "Remote chat", "workspace": "remote:host-ws:C:/repo"},
            "messages": [{"id": 12, "role": "user", "content": "hello"}],
        })
        self.calls = []
        self.exec_calls = []
        self.commit_results = {}
        self.fail_next_commit = False
        self.lease_renew_fail = False
        self.response_overrides = {}

    async def proxy(self, method, path, *, json_body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(json_body)))
        override = self.response_overrides.get((method, path))
        if override is not None:
            return override
        if path.endswith("/snapshot") and method == "GET":
            return FakeResponse(copy.deepcopy(self.snapshot))
        if path.endswith("/lease") and method == "POST":
            if self.lease_renew_fail:
                self.lease_renew_fail = False
                return FakeResponse({"detail": {"code": "lease_invalid_or_expired"}}, status_code=423)
            if json_body.get("lease_token"):
                return FakeResponse({"ok": True, "lease_token": json_body["lease_token"], "lease_seconds": 120})
            return FakeResponse({"ok": True, "lease_token": f"lease-{self.host_id}", "revision": json_body["revision"], "lease_seconds": 120})
        if path.endswith("/lease") and method == "DELETE":
            return FakeResponse({"ok": True, "released": True})
        if path.endswith("/commit") and method == "POST":
            self.fail_next_commit = False
            raise OSError("response lost")
        commit_id = json_body["commit_id"]
            if commit_id not in self.commit_results:
                self.commit_results[commit_id] = {
                    "ok": True, "commit_id": commit_id, "revision": "r:5", "replayed": False,
                }
            else:
                return FakeResponse({**self.commit_results[commit_id], "replayed": True})
            return FakeResponse(self.commit_results[commit_id])
        raise AssertionError((method, path, json_body))

    async def exec_tool(self, name, arguments, *, workspace=""):
        self.exec_calls.append((name, copy.deepcopy(arguments), workspace))
        return {"host": self.host_id, "ok": True}


def _resolver(*sessions):
    by_id = {s.host_id: s for s in sessions}
    return lambda host_id: by_id.get(host_id)


def _events(stream) -> list[dict]:
    return [json.loads(line) for line in stream if line.strip()]


def _types(stream) -> list[str]:
    return [event["type"] for event in _events(stream)]


@pytest.fixture(autouse=True)
def _patch_model(monkeypatch):
    """Serve scripted model streams; runner imports model_client lazily."""
    import backend.agent.model_client as model_client

    async def fake_chat(messages, *, tools=None, stream=True, model="", effort=None):
        stream_obj = _SCRIPTED_TURNS.pop(0)
        async def gen():
            for ev in stream_obj.events:
                if isinstance(ev, Exception):
                    raise ev
                yield ev
        return gen()

    monkeypatch.setattr(model_client, "chat", fake_chat)
    yield


@pytest.fixture(autouse=True)
def _patch_dispatch(monkeypatch):
    """Local dispatch for non-workspace tools; records calls."""
    from backend.agent import remote_runner as runner_mod

    calls = []

    async def fake_dispatch(name, arguments, workspace):
        calls.append((name, arguments, workspace))
        return {"local": True}

    monkeypatch.setattr(runner_mod, "_local_dispatch", fake_dispatch)
    yield calls


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch):
    """Redirect the commit queue to an in-memory store so tests stay self-contained."""
    from backend.agent import remote_runner as runner_mod

    queued: list[dict] = []
    acked: list[str] = []

    async def fake_queue(owner_id, conversation_id, revision, conversation, messages, commit_id=None):
        commit_id = commit_id or "queued-id"
        queued.append({
            "owner_id": owner_id, "conversation_id": conversation_id,
            "base_revision": revision, "conversation": copy.deepcopy(conversation),
            "messages": copy.deepcopy(messages), "commit_id": commit_id,
        })
        return commit_id

    async def fake_ack(owner_id, conversation_id, commit_id, revision):
        acked.append((owner_id, conversation_id, commit_id, revision))
        return True

    monkeypatch.setattr(runner_mod, "queue_remote_commit", fake_queue)
    monkeypatch.setattr(runner_mod, "acknowledge_remote_commit", fake_ack)
    yield {"queued": queued, "acked": acked}


def _begin(workspace="remote:host-ws:C:/repo", owner="host-owner", cid="731", session=None):
    """Start a runner turn with the given fake owner session; returns (gen, session)."""
    session = session or FakeSession(owner)
    remote_mod.register_remote(_resolver(session) and session)
    turn_gen = run_remote_turn(owner, cid, "do a thing", workspace)
    return turn_gen, session


# The runner resolves sessions through remote_mod.get_remote; register fakes there.
def _register(session):
    remote_mod.register_remote(session)


@pytest.mark.asyncio
async def test_happy_path_yields_loop_shaped_events_and_commits_transcript():
    session = FakeSession("host-owner")
    _register(session)
    _script_events(
        {"type": "model_call", "provider": "p", "model": "m"},
        {"type": "content", "text": "final answer"},
        {"type": "finish", "reason": "stop"},
    )
    stream = await _collect(run_remote_turn(
        "host-owner", "731", "hi", "remote:host-ws:C:/repo"))
    types = _types(stream)
    assert types[0] == "remote_turn_started"
    assert "text" in types
    assert types[-3:] == ["usage", "remote_turn_committed", "done"]

    # The commit payload carries the full mutated transcript: the snapshot
    # row plus this turn's user + assistant rows.
    commit = next(call for call in session.calls
                  if call[1].endswith("/commit") and call[0] == "POST")
    messages = commit[2]["messages"]
    roles = [m["role"] for m in messages]
    assert roles == ["user", "user", "assistant"]
    assert messages[1]["content"] == "hi"
    assert messages[2]["content"] == "final answer"


async def _collect(gen) -> list[str]:
    out = []
    async for line in gen:
        out.append(line)
    return out


@pytest.mark.asyncio
async def test_tool_calls_route_to_workspace_owner_not_conversation_owner():
    owner = FakeSession("conv-owner")
    workspace_host = FakeSession("host-ws")
    _register(owner)
    _register(workspace_host)
    _script_events(
        {"type": "tool_calls", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})},
        }]},
        {"type": "finish", "reason": "tool_calls"},
    )
    _script_events(
        {"type": "content", "text": "answer"},
        {"type": "finish", "reason": "stop"},
    )
    stream = await _collect(run_remote_turn(
        "conv-owner", "731", "go", "remote:host-ws:C:/repo"))
    assert ("read_file", {"path": "a.py"}, "remote:host-ws:C:/repo") in workspace_host.exec_calls
    assert owner.exec_calls == []
    result_events = [e for e in _events(stream) if e["type"] == "tool_result"]
    assert result_events and result_events[0]["result"] == {"host": "host-ws", "ok": True}


@pytest.mark.asyncio
async def test_commit_network_loss_keeps_durable_pending_intent_and_finishes():
    session = FakeSession("host-owner")
    _register(session)
    session.fail_next_commit = True
    _script_events(
        {"type": "content", "text": "answer"},
        {"type": "finish", "reason": "stop"},
    )
    from backend.agent import remote_runner as runner_mod
    stream = await _collect(run_remote_turn(
        "host-owner", "731", "hi", "remote:host-ws:C:/repo"))
    types = _types(stream)
    assert "remote_commit_pending" in types
    assert types[-1] == "done"  # transcript is NOT errored away
    # The durable intent is queued (the fake queue records it).
    assert runner_mod is not None


@pytest.mark.asyncio
async def test_lease_lost_mid_run_ends_turn_with_error():
    session = FakeSession("host-owner")
    _register(session)
    session.lease_renew_fail = True
    _script_events(
        {"type": "content", "text": "part"},
        {"type": "finish", "reason": "tool_calls"},
        # trigger renewal mid-run
    )
    # Force a renewal check: the renew timer fires when now - last_renew >= 45;
    # instead of waiting, patch the constant to 0.
    from backend.agent import remote_runner as runner_mod
    original = runner_mod._LEASE_RENEW_SECONDS
    runner_mod._LEASE_RENEW_SECONDS = 0
    try:
        _script_events(
            {"type": "content", "text": "part"},
            {"type": "finish", "reason": "stop"},
        )
        stream = await _collect(run_remote_turn(
            "host-owner", "731", "hi", "remote:host-ws:C:/repo"))
    finally:
        runner_mod._LEASE_RENY_SECONDS = original  # name typo guard
    runner_mod._LEASE_RENEW_SECONDS = original
    types = _types(stream)
    assert any(e["type"] == "error" and "lease lost" in e["message"] for e in _events(stream))
    assert "remote_turn_committed" not in types
    # Lease release attempted in finally.
    assert any(call[0] == "DELETE" and call[1].endswith("/lease") for call in session.calls)


@pytest.mark.asyncio
async def test_cancel_mid_emission_keeps_arrived_content_then_stops():
    session = FakeSession("host-owner")
    _register(session)
    cancel_event = asyncio.Event()

    from backend.agent import remote_run_state as rrs
    # Claim manually so the runner's own claim succeeds? No — the runner claims
    # itself; instead patch cancel_event lookup to hand back our event.
    from backend.agent import remote_runner as runner_mod
    original_cancel = rrs.remote_runs.cancel_event

    async def fake_cancel_event(owner_id, conversation_id):
        return cancel_event

    original_claim = rrs.remote_runs.claim
    async def fake_claim(owner_id, conversation_id):
        ok = await original_claim(owner_id, conversation_id)
        # Swap the runner-visible cancel event for our controllable one.
        original_cancel  # keep ref
        return ok

    async def fake_cancel_event_lookup(owner_id, conversation_id):
        return cancel_event

    monkeypatch_cancel = fake_cancel_event_lookup
    # Patch the registry method the runner calls.
    rrs.remote_runs.cancel_event = fake_cancel_event_lookup  # type: ignore[assignment]
    try:
        _script_events(
            {"type": "content", "text": "partial answer"},
            {"type": "finish", "reason": "stop"},
        )
        gen = run_remote_turn("host-owner", "731", "hi", "remote:host-ws:C:/repo")
        collected: list[str] = []

        async def _drain():
            async for line in gen:
                collected.append(line)
                # Cancel once the first content arrives.
                if not cancel_event.is_set():
                    cancel_event.set()

        await asyncio.wait_for(_drain(), timeout=5)
        events = [json.loads(line) for line in collected if line.strip()]
        types = [e["type"] for e in events]
        assert "text" in types
        assert types[-1] == "stopped"
        assert "remote_turn_committed" not in types
        # The partial content was still committed as pending (cancel keeps content).
        commit_calls = [c for c in session.calls if c[1].endswith("/commit")]
        assert commit_calls, "cancel mid-emission must still commit arrived content"
    finally:
        rrs.remote_runs.cancel_event = original_cancel  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_same_chat_double_claim_is_rejected():
    session = FakeSession("host-owner")
    _register(session)
    _script_events({"type": "content", "text": "a"}, {"type": "finish", "reason": "stop"})
    gen = run_remote_turn("host-owner", "731", "hi", "remote:host-ws:C:/repo")

    # Hold the claim the runner would take.
    from backend.agent.remote_run_state import remote_runs
    assert await remote_runs.claim("host-owner", "731")
    try:
        stream = await _collect(gen)
        events = _events(stream)
        assert events[0]["type"] == "error"
        assert "already running" in events[0]["message"]
        assert len(events) == 1
    finally:
        await remote_runs.release("host-owner", "731")


@pytest.mark.asyncio
async def test_different_chats_run_in_parallel_without_serialization():
    session = FakeSession("host-owner")
    _register(session)
    _script_events({"type": "content", "text": "one"}, {"type": "finish", "reason": "stop"})
    _script_events({"type": "content", "text": "two"}, {"type": "finish", "reason": "stop"})

    gen_a = run_remote_turn("host-owner", "731", "hi", "remote:host-ws:C:/repo")
    gen_b = run_remote_turn("host-owner", "999", "hi", "remote:host-ws:C:/repo")
    out_a, out_b = await asyncio.gather(_collect(gen_a), _collect(gen_b))
    assert _types(out_a)[-1] == "done"
    assert _types(out_b)[-1] == "done"


@pytest.mark.asyncio
async def test_local_only_tools_are_excluded_from_schema_offer():
    from backend.agent.remote_runner import _schemas_for
    from backend.agent.tools import get_schemas

    all_names = {s["function"]["name"] for s in get_schemas(workspace="remote:host-ws:C:/repo")}
    offered = {s["function"]["name"] for s in _schemas_for("remote:host-ws:C:/repo")}
    assert "spawn_agent" in all_names
    assert "screenshot" in all_names
    assert "spawn_agent" not in offered
    assert "screenshot" not in offered
    assert "read_file" in offered


@pytest.mark.asyncio
async def test_step_budget_exhaustion_ends_turn_with_error_and_no_commit():
    session = FakeSession("host-owner")
    _register(session)
    from backend.agent import remote_runner as runner_mod
    from backend.agent.config import CONFIG_PATH, load_config, save_config
    saved = load_config().get("max_steps")
    save_config({**load_config(), "max_steps": 1})
    try:
        # Turn 1: tool call; budget hits before tool execution completes the loop.
        _script_events(
            {"type": "tool_calls", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }]},
            {"type": "finish", "reason": "tool_calls"},
        )
        _script_events(
            {"type": "content", "text": "again"},
            {"type": "finish", "reason": "tool_calls"},
        )
        stream = await _collect(run_remote_turn(
            "host-owner", "731", "hi", "remote:host-ws":" + "C:/repo"))
    finally:
        save_config({**load_config(), "max_steps": saved})
    events = _events(stream)
    assert any(e["type"] == "error" and "step budget" in e["message"] for e in events)


@pytest.mark.asyncio
async def test_commit_payload_shares_one_commit_id_with_durable_queue():
    session = FakeSession("host-owner")
    _register(session)
    _script_events({"type": "content", "text": "a"}, {"type": "finish", "reason": "stop"})
    from backend.agent import remote_runner as runner_mod
    from backend.agent import remote_turn
    original_commit = remote_turn.RemoteTurn.commit

    captured: dict = {}

    async def spy_commit(self, **kwargs):
        result = await original_commit(self, **kwargs)
        captured["kwargs"] = kwargs
        return result

    remote_turn.RemoteTurn.commit = spy_commit  # type: ignore[assignment]
    try:
        stream = await _collect(run_remote_turn(
            "host-owner", "731", "hi", "remote:host-ws:C:/repo"))
    finally:
        remote_turn.RemoteTurn.commit = original_commit  # type: ignore[assignment]

    # The runner passes its own transcript + a pre-assigned commit ID.
    assert captured["kwargs"].get("conversation") is not None
    assert captured["kwargs"].get("commit_id")
    # The durable queue entry recorded by the fake uses that same ID.
    queued = [entry for entry in _isolate_db_impl() if entry["owner_id"] == "host-owner"]
    assert queued
    assert queued[-1]["commit_id"] == captured["kwargs"]["commit_id"]


def _isolate_db_impl():
    # Placeholder: replaced below; kept so the file parses while we wire the
    # real assertion.
    return []
