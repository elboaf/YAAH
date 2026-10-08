"""#357: teardown hardening — shielded retirement + hourly sweep ticker.

Two failure modes verified in the #353 review, both fixed here:

- Post-run retirement sat unshielded in run_agent's finally
  (loop.py); ``except Exception`` does not catch CancelledError, so a
  Stop press aborted retirement mid-git-call with no log and no retry
  path. The retirement now runs under a bounded shield (the sub-agent
  grace pattern): cancellation is absorbed for the git calls' sake,
  and a timeout/miss enqueues the tree for the sweep instead of a
  silent stand.
- ``should_sweep`` promised "once per boot, then at most hourly" but
  nothing re-checked it: main.py swept exactly once per boot, so
  landed-clean trees stood for a day. The ticker now keeps the hourly
  promise, and enqueued trees retire on the next tick without the age
  or dead-chat gates (the post-run hook retires live chats' trees by
  design; only the clean/no-residue gates still decide).

The cancel tests exercise the same seam run_agent's finally calls
(``_retire_*_chat_worktree_shielded``) against REAL temp-repo
worktrees; the full run_agent generator is downstream wiring, not the
mechanism under test.
"""
import asyncio
import subprocess

import pytest

from backend.agent import loop as loop_mod
from backend.agent import worktrees, wt_sweep


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _repo_with_commit(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


async def _materialize(repo, cid=77):
    return await worktrees.ensure_chat_worktree(str(repo), cid, "master")


def _enqueued_for(ws):
    return wt_sweep._enqueued.get(ws, set())


@pytest.mark.asyncio
async def test_cancel_mid_teardown_shield_still_retires(tmp_path, monkeypatch):
    """AC: cancel at the retirement await => the worktree still retires
    (shield). The awaited git call outlives the cancelled teardown
    caller; nothing is enqueued because nothing was missed."""
    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]
    assert chat_dir.exists()

    gate, release = asyncio.Event(), asyncio.Event()
    orig = worktrees.retire_chat_worktree

    async def slow_retire(ws, cid, **kw):
        gate.set()
        await release.wait()
        return await orig(ws, cid, **kw)

    monkeypatch.setattr(worktrees, "retire_chat_worktree", slow_retire)

    task = asyncio.create_task(
        loop_mod._retire_local_chat_worktree_shielded(str(repo), 77)
    )
    await asyncio.wait_for(gate.wait(), 5)  # retirement is mid-git-call
    task.cancel()  # the Stop press: teardown dies HERE
    release.set()  # ...but the shielded git region still completes
    with pytest.raises(asyncio.CancelledError):
        await task  # raises only AFTER the awaited calls finished

    # The shielded inner task survived the cancellation and finished.
    assert not chat_dir.exists(), "shielded retirement did not complete"
    assert str(77) not in _enqueued_for(str(repo))


@pytest.mark.asyncio
async def test_retirement_timeout_enqueues_and_next_tick_retires(
    tmp_path, monkeypatch
):
    """AC: retirement that cannot finish inside the grace window (git
    wedged) enqueues the tree; the next sweep tick retires it. No
    silent stand."""
    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]

    calls = {"n": 0}

    async def wedged_retire(ws, cid, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(0.3)  # wedged past the grace window
        return {"retired": False, "reason": "simulated wedge"}

    # Scoped: only the teardown call runs wedged; the sweep phase below
    # exercises the REAL hook.
    with monkeypatch.context() as m:
        m.setattr(worktrees, "retire_chat_worktree", wedged_retire)
        m.setattr(
            loop_mod, "TEARDOWN_RETIREMENT_GRACE_SECONDS", 0.05
        )

        # Must NOT raise: the timeout is absorbed, the miss is recorded.
        await loop_mod._retire_local_chat_worktree_shielded(str(repo), 77)
    assert chat_dir.exists()
    assert "77" in _enqueued_for(str(repo))

    # Next sweep tick: the real hook retires the enqueued tree, no age
    # gate (the tree was in use moments ago).
    result = await wt_sweep.sweep_enqueued_retirements()
    assert str(chat_dir) in result["swept"], result
    assert not chat_dir.exists()
    assert not _enqueued_for(str(repo))


@pytest.mark.asyncio
async def test_git_remove_failure_enqueues_and_next_tick_retires(
    tmp_path, monkeypatch
):
    """AC: rc != 0 from `git worktree remove` => enqueue happens, next
    sweep tick retires."""
    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]

    orig = worktrees._run_git

    async def failing_remove(anchor, *args):
        if "remove" in args:
            return 1, "fatal: simulated worktree remove failure"
        return await orig(anchor, *args)

    with monkeypatch.context() as m:
        m.setattr(worktrees, "_run_git", failing_remove)
        await loop_mod._retire_local_chat_worktree_shielded(str(repo), 77)
    assert chat_dir.exists()
    assert "77" in _enqueued_for(str(repo))

    result = await wt_sweep.sweep_enqueued_retirements()
    assert str(chat_dir) in result["swept"], result
    assert not chat_dir.exists()


@pytest.mark.asyncio
async def test_policy_refusals_do_not_enqueue(tmp_path):
    """A dirty tree is the residue protocol's to surface, not a missed
    retirement: no enqueue, no sweep retry against a policy refusal."""
    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]
    (chat_dir / "scratch.txt").write_text("wip\n", encoding="utf-8")

    await loop_mod._retire_local_chat_worktree_shielded(str(repo), 77)
    assert chat_dir.exists()
    assert not _enqueued_for(str(repo))

    result = await wt_sweep.sweep_enqueued_retirements()
    assert result["swept"] == []
    assert chat_dir.exists()


@pytest.mark.asyncio
async def test_remote_misses_enqueue_and_retry_on_tick(monkeypatch):
    """AC (remote leg): an unreachable host enqueues; 'no chat
    worktree' drops the entry. The retry goes through
    wt_remote.retire_chat_worktree."""
    ws = "remote://box/proj"
    monkeypatch.setattr(
        wt_sweep, "_enqueued", {ws: {"21", "22"}}, raising=False
    )

    results = {
        "21": {"retired": False, "reason": "host unreachable"},
        "22": {"retired": False, "reason": "no chat worktree"},
    }

    async def fake_remote_retire(workspace, chat_id):
        return results[str(chat_id)]

    monkeypatch.setattr(
        "backend.agent.wt_remote.retire_chat_worktree", fake_remote_retire
    )

    result = await wt_sweep.sweep_enqueued_retirements()
    # unreachable: entry stays for the next tick
    assert "21" in _enqueued_for(ws)
    assert f"{ws} chat-21" in result["kept"]
    # no chat worktree: nothing left to retire, entry dropped
    assert "22" not in _enqueued_for(ws)
    assert f"{ws} chat-22" in result["dropped"]

    # Next tick: host is back, the missed tree retires via the gateway.
    results["21"] = {"retired": True, "path": "unused"}
    result = await wt_sweep.sweep_enqueued_retirements()
    assert f"{ws} chat-21" in result["swept"]
    assert not _enqueued_for(ws)


@pytest.mark.asyncio
async def test_remote_cancel_shield_still_retires(monkeypatch):
    """Remote twin of the Stop-press case: cancellation at the
    retirement await is absorbed by the shield and the gateway call
    completes."""
    ws = "remote://box/proj"
    gate, release = asyncio.Event(), asyncio.Event()
    done = asyncio.Event()

    async def slow_remote_retire(workspace, chat_id):
        gate.set()
        await release.wait()
        done.set()
        return {"retired": True, "path": "unused"}

    monkeypatch.setattr(
        "backend.agent.wt_remote.retire_chat_worktree", slow_remote_retire
    )

    task = asyncio.create_task(
        loop_mod._retire_remote_chat_worktree_shielded(ws, 55)
    )
    await asyncio.wait_for(gate.wait(), 5)
    task.cancel()
    release.set()  # the shielded gateway call still completes
    with pytest.raises(asyncio.CancelledError):
        await task  # raises only AFTER the awaited call finished
    assert done.is_set()
    assert "55" not in _enqueued_for(ws)


def test_should_sweep_fires_again_an_hour_later():
    """AC: `should_sweep` fires once, rate-limits inside the hour, and
    fires again ~1h later inside one process lifetime (fake clock)."""
    saved = wt_sweep._last_sweep
    try:
        wt_sweep._last_sweep = 0.0
        assert wt_sweep.should_sweep(1000.0) is True  # boot sweep
        assert wt_sweep.should_sweep(1000.0 + 3599.0) is False
        assert wt_sweep.should_sweep(1000.0 + 3600.0) is True
        assert wt_sweep.should_sweep(1000.0 + 3600.0 + 60.0) is False
    finally:
        wt_sweep._last_sweep = saved


@pytest.mark.asyncio
async def test_ticker_ticks_periodically_and_start_is_idempotent(
    monkeypatch,
):
    """The ticker keeps the hourly promise: once started it re-checks
    the sweep window forever, and starting it twice does not double
    it."""
    ticks = []
    monkeypatch.setattr(wt_sweep, "_MIN_INTERVAL", 0.05)

    async def fake_tick():
        ticks.append(1)

    monkeypatch.setattr(wt_sweep, "_sweep_ticker_tick", fake_tick)

    wt_sweep.start_sweep_ticker()
    first = wt_sweep._ticker_task
    assert first is not None
    wt_sweep.start_sweep_ticker()  # idempotent
    assert wt_sweep._ticker_task is first

    try:
        await asyncio.wait_for(first, timeout=2.0)
    except asyncio.TimeoutError:
        pass
    assert len(ticks) >= 2, f"ticker did not tick: {ticks}"

    await wt_sweep.stop_sweep_ticker()
    assert wt_sweep._ticker_task is None


@pytest.mark.asyncio
async def test_tick_runs_enqueued_retirements_without_age_gate(
    tmp_path, monkeypatch
):
    """The shared tick drives BOTH legs: the age-gated sweep (as
    before) and the #357 enqueue retry, which skips the age gate."""
    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]
    wt_sweep.enqueue_missed_retirement(str(repo), 77)

    calls = []

    async def fake_age_gated():
        calls.append("age-gated")
        return {"checked": 0, "swept": [], "kept": 0, "reaped_branches": []}

    monkeypatch.setattr(wt_sweep, "sweep_stale_chat_worktrees", fake_age_gated)
    monkeypatch.setattr(wt_sweep, "should_sweep", lambda now=None: True)

    await wt_sweep._sweep_ticker_tick()
    assert "age-gated" in calls
    assert not chat_dir.exists()
    assert not _enqueued_for(str(repo))
