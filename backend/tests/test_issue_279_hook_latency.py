"""Issue #279: the WH_*_LL hook callback latency is the direct proxy metric
for cursor lag, so the activity tracker must record dispatch durations and
expose percentile summaries — and the gitinfo poll burst must not contend
with the agent's index.lock while a run is writing the worktree."""
import asyncio
import time

import pytest

from backend.agent import computer as computer_mod
from backend.agent import gitinfo


# ---------------------------------------------------------------- hook latency


def _fresh_activity():
    return computer_mod._Activity()


def test_note_records_dispatch_latency():
    act = _fresh_activity()
    act._note(elapsed_ms=0.42)
    act._note(elapsed_ms=1.7)
    assert act.latency_summary_ms() == {"p50": 0.42, "p95": 1.7, "p99": 1.7}


def test_note_without_latency_is_free():
    """The zero-arg call is the pre-existing behavior (tests/live calls);
    it must not pollute the latency sample set."""
    act = _fresh_activity()
    act._note()
    assert act.latency_summary_ms() is None


def test_latency_ring_buffer_is_bounded():
    act = _fresh_activity()
    for i in range(10_000):
        act._note(elapsed_ms=float(i % 100))
    # Bounded memory: never grows past the ring size.
    assert act.latency_summary_ms()["p50"] is not None
    assert len(act._latencies) == act._LATENCY_RING


def test_latency_summary_empty_is_none():
    assert _fresh_activity().latency_summary_ms() is None


def test_snapshot_includes_latency_summary():
    """The health/diag surface must carry the numbers the regression script
    reads, so no test can pass with instrumentation that nothing exposes."""
    act = _fresh_activity()
    act._started = True
    act._note(elapsed_ms=0.9)
    snap = act.snapshot()
    assert snap["started"] is True
    assert snap["latency_ms"] == {"p50": 0.9, "p95": 0.9, "p99": 0.9}


@pytest.mark.skipif(not computer_mod.WINDOWS, reason="windows-only hook timing")
def test_hook_callback_measures_dispatch(monkeypatch):
    """The real callback path wraps its work in the timing wrapper."""
    act = _fresh_activity()

    class FakeUser32:
        calls = []

        def CallNextHookEx(self, *a):
            FakeUser32.calls.append(time.perf_counter())
            return 0

    user32 = FakeUser32()
    called = []
    original_note = act._note

    def spying_note(elapsed_ms=None):
        called.append(elapsed_ms)
        if elapsed_ms is not None:
            original_note(elapsed_ms)

    monkeypatch.setattr(act, "_note", spying_note)
    cb = computer_mod._make_timed_callback(
        act, computer_mod._KBDLLHOOKSTRUCT, computer_mod._LLKHF_INJECTED,
        lambda: user32,
    )
    import ctypes
    struct = computer_mod._KBDLLHOOKSTRUCT()
    struct.flags = 0  # real input
    buf = (ctypes.c_char * 64)()
    import ctypes.wintypes
    ctypes.memmove(buf, ctypes.byref(struct), ctypes.sizeof(struct))
    cb(0, 0x0100, ctypes.addressof(buf))
    # First: the untimed real-input record (pre-existing behavior); second:
    # the dispatch-latency sample taken around the whole callback body.
    assert called == [None] or (len(called) == 2 and called[1] is not None), called
    assert called[-1] is not None


# ---------------------------------------------------------------- gitinfo locks


@pytest.mark.asyncio
async def test_gitinfo_burst_skips_index_lock(monkeypatch, tmp_path):
    """Every read-only git spawn from gitinfo must pass --no-optional-locks
    so the UI's 2s poll never blocks on (or blocks) the agent's index.lock
    while a run is writing the worktree."""
    seen = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        seen.append(args)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    try:
        await gitinfo.git_workspace_info(tmp_path)
    finally:
        gitinfo._info_cache.pop(str(tmp_path), None)
        gitinfo._cache.pop(str(tmp_path), None)
    assert seen, "expected the info burst to spawn git"
    for args in seen:
        git_args = list(args)[1:]  # drop the program name
        assert git_args[0] == "--no-optional-locks", args


@pytest.mark.asyncio
async def test_branch_poll_skips_index_lock(monkeypatch, tmp_path):
    seen = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        seen.append(args)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/stale\n")
    try:
        await gitinfo.current_git_branch(tmp_path)
    finally:
        gitinfo._cache.pop(str(tmp_path), None)
    assert seen and list(seen[0])[1] == "--no-optional-locks"
