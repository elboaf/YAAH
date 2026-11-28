"""Endpoint-level regression for #903: the remote-device turn endpoint must
claim the run slot atomically before responding.

The pre-fix shape checked `is_running` in the handler and claimed inside
the stream generator. Two consequences: two concurrent sends both passed
the check (the second failed mid-stream instead of 409ing), and a fresh
send arriving while a finished/abandoned turn's stream was still tearing
down 409'd even though the client saw no live run (the "ephemeral 409"
reported on the #842 thin-client progress update).

The fix claims in the handler via `claim_with_grace` (a grace window that
rides out stale-claim teardown) and passes `claimed=True` to the runner,
so the runner releases exactly once.

Tested through the real endpoint with TestClient; the turn body is stubbed
(the seam under test is the claim/409 ordering, not the turn loop). The
`list_remote_conversations` stub doubles as the in-loop setup point: it
runs on the same event loop the endpoint's claim uses, so pre-claiming
there is loop-correct.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.agent.remote_run_state import remote_runs as real_runs


WS = "remote:host-ws:C:/repo"
PATH = "/api/remote/devices/host-a/turns/731"


class _ShortGraceRuns:
    """Delegates to the real registry with a short grace window so the
    teardown-ride-out tests stay fast (real default is 2.0 s)."""

    def __getattr__(self, name):
        return getattr(real_runs, name)

    async def claim_with_grace(self, owner_id, conversation_id,
                               grace_seconds=2.0, poll_seconds=0.1):
        return await real_runs.claim_with_grace(
            owner_id, conversation_id, grace_seconds=0.5, poll_seconds=0.05)


@pytest.fixture
def _stub_turn(monkeypatch):
    """Record the runner call; the handler must pass claimed=True."""
    from backend.agent import remote_runner as runner_mod

    calls = []

    async def fake_turn(owner_id, conversation_id, message, workspace="",
                        model_override="", effort_override=None, claimed=False):
        calls.append({"claimed": claimed, "workspace": workspace})
        yield '{"type": "done"}\n'

    monkeypatch.setattr(runner_mod, "run_remote_turn", fake_turn)
    yield calls


@pytest.fixture
def _short_grace(monkeypatch):
    from backend.agent import remote_runner as runner_mod
    monkeypatch.setattr(runner_mod, "remote_runs", _ShortGraceRuns())


def _workspace_stub(monkeypatch, *, pre_claim=False, release_after=None):
    """Stub the cached-row lookup; optionally pre-hold the claim on the
    endpoint's own event loop (simulating a live or still-tearing-down
    turn) before the handler's claim runs."""
    import backend.main as mainmod

    async def fake_list(owner_id):
        if pre_claim:
            assert await real_runs.claim(owner_id, "731")
            if release_after is not None:
                async def _release_soon():
                    await asyncio.sleep(release_after)
                    await real_runs.release(owner_id, "731")
                asyncio.get_running_loop().create_task(_release_soon())
        return [{"conversation_id": "731", "workspace": WS}]

    monkeypatch.setattr(mainmod, "list_remote_conversations", fake_list)


@pytest.fixture
def _clean_runs():
    """Release any claim the (stubbed, non-releasing) turn left behind."""
    yield
    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False
    if running:
        return
    try:
        asyncio.run(real_runs.release("host-a", "731"))
    except RuntimeError:
        pass


def _post(client, body=None):
    return client.post(PATH, json=body or {"message": "hi"})


def test_fresh_send_succeeds_and_handler_holds_the_claim(
        monkeypatch, _stub_turn, _short_grace, _clean_runs):
    """AC: a send after a completed turn succeeds; the runner receives
    claimed=True (it must not re-claim) and the resolved workspace."""
    _workspace_stub(monkeypatch)
    with TestClient(__import__("backend.main", fromlist=["app"]).app) as client:
        r = _post(client)
        assert r.status_code == 200, r.text
        assert "done" in r.text
    assert _stub_turn and _stub_turn[0]["claimed"] is True
    assert _stub_turn[0]["workspace"] == WS


def test_live_run_still_409s_after_the_grace(
        monkeypatch, _stub_turn, _short_grace, _clean_runs):
    """AC: a genuinely live run (claim held past the grace) still gets a
    clean 409 with the same detail — the fix must not swallow the real
    conflict. The stubbed turn never releases, so the pre-held claim
    outlives the grace window."""
    _workspace_stub(monkeypatch, pre_claim=True)
    with TestClient(__import__("backend.main", fromlist=["app"]).app) as client:
        r = _post(client)
        assert r.status_code == 409, r.text
        assert "already running" in r.json()["detail"]
    # The runner was never invoked — the refusal happened before the stream.
    assert _stub_turn == []


def test_send_rides_out_stale_claim_tearing_down(
        monkeypatch, _stub_turn, _short_grace, _clean_runs):
    """AC (the owner's repro on #842): the client sees no live run, but the
    previous turn's stream is still tearing down — its claim is held, then
    released ~0.3 s later. The grace window must ride it out; the send
    succeeds instead of 409ing."""
    _workspace_stub(monkeypatch, pre_claim=True, release_after=0.3)
    with TestClient(__import__("backend.main", fromlist=["app"]).app) as client:
        r = _post(client)
        assert r.status_code == 200, r.text
        assert "done" in r.text
    assert _stub_turn and _stub_turn[0]["claimed"] is True


def test_missing_workspace_400s_without_claiming(
        monkeypatch, _stub_turn, _short_grace, _clean_runs):
    """The workspace 400 is decided before the claim (pure read), so a bad
    request never touches the run slot."""
    import backend.main as mainmod

    async def empty_list(owner_id):
        return []

    monkeypatch.setattr(mainmod, "list_remote_conversations", empty_list)
    with TestClient(mainmod.app) as client:
        r = _post(client)
        assert r.status_code == 400, r.text
    assert _stub_turn == []
