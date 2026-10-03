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


def test_record_latency_samples_and_summary():
    act = _fresh_activity()
    act._record_latency(0.42)
    act._record_latency(1.7)
    assert act.latency_summary_ms() == {"p50": 0.42, "p95": 1.7, "p99": 1.7}


def test_record_latency_does_not_touch_idle_time():
    """Latency samples come from every hook callback, including the agent's
    own injected input (and negative-ncode calls). They must never reset
    idle_seconds(): the user-active pause treats agent input as real user
    input otherwise."""
    act = _fresh_activity()
    act._started = True
    act._note()
    act._last = time.monotonic() - 500.0  # last real input 500s ago
    act._record_latency(0.5)
    assert act.idle_seconds() >= 500.0


def test_note_without_latency_is_free():
    """The zero-arg call is the pre-existing behavior (tests/live calls);
    it must not pollute the latency sample set."""
    act = _fresh_activity()
    act._note()
    assert act.latency_summary_ms() is None


def test_latency_ring_buffer_is_bounded():
    act = _fresh_activity()
    for i in range(10_000):
        act._record_latency(float(i % 100))
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
    act._record_latency(0.9)
    snap = act.snapshot()
    assert snap["started"] is True
    assert snap["latency_ms"] == {"p50": 0.9, "p95": 0.9, "p99": 0.9}


def test_hook_install_failures_reports_missing_hooks():
    """The measurement script must refuse to report numbers when one of the
    two hooks did not install: mouse-only samples would look healthy while
    the keyboard hook chain is unmeasured (CodeRabbit, issue #279)."""
    from backend.scripts.measure_hook_latency import _hook_install_failures

    assert _hook_install_failures({"mouse": True, "keyboard": True}) == []
    assert _hook_install_failures({"mouse": True, "keyboard": False}) == [
        "keyboard"]
    assert _hook_install_failures({"mouse": False, "keyboard": False}) == [
        "keyboard", "mouse"]


def test_hook_install_failures_absent_key_is_failure():
    """A hook thread that never reported (crashed, or still installing) is
    not a success: the failure check must judge each expected hook name,
    not just the keys that happen to exist in the mapping."""
    from backend.scripts.measure_hook_latency import (
        _EXPECTED_HOOKS,
        _hook_install_failures,
    )

    assert set(_EXPECTED_HOOKS) == {"mouse", "keyboard"}
    assert _hook_install_failures({}) == ["keyboard", "mouse"]
    assert _hook_install_failures({"mouse": True}) == ["keyboard"]
    assert _hook_install_failures(
        {"mouse": True, "keyboard": False}) == ["keyboard"]


def test_hook_install_failures_empty_means_nothing_tried_is_failure():
    from backend.scripts.measure_hook_latency import _hook_install_failures

    # Kept as a regression pin for the old (absent==success) semantics:
    # an empty mapping must now read as both hooks missing, per
    # test_hook_install_failures_absent_key_is_failure.
    assert _hook_install_failures({}) != []


# ------------------------------------------------- keyboard hook exercise


@pytest.mark.skipif(not computer_mod.WINDOWS,
                    reason="real LL hooks need ctypes.windll")
def test_keyboard_events_are_injected_and_measured_separately(monkeypatch):
    """The regression gate must verify samples for EACH hook, not a shared
    p99 a mouse-only run can satisfy: the keyboard hook is installed, so it
    gets its own injected keyboard traffic and its own sample check."""
    import backend.scripts.measure_hook_latency as m

    measured = {}

    def fake_measure(activities, seconds):
        names = [n for n in activities]
        measured["names"] = names
        return {n: {"p50": 0.1, "p95": 0.2, "p99": 0.3} for n in names}

    monkeypatch.setattr(m, "_measure_hooks", fake_measure)

    rc, summaries = m._run_measurement(seconds=1.0, budget_ms=5.0)
    assert "mouse" in measured["names"]
    assert "keyboard" in measured["names"]
    assert set(summaries) == {"mouse", "keyboard"}
    assert rc == 0


def test_keyboard_hook_without_samples_fails_even_if_mouse_is_healthy():
    """The pass gate is per hook: a keyboard run with zero keyboard samples
    must fail even though the mouse p99 is well inside budget."""
    import backend.scripts.measure_hook_latency as m

    rc, summaries = m._judge(
        {"mouse": {"p50": 0.1, "p95": 0.2, "p99": 0.3}, "keyboard": None},
        budget_ms=5.0)
    assert rc == 1
    assert "keyboard" in " ".join(str(x) for x in (summaries or []))


@pytest.mark.skipif(not computer_mod.WINDOWS, reason="windows-only hook timing")
def test_hook_callback_measures_dispatch(monkeypatch):
    """The real callback path wraps its work in the timing wrapper."""
    act = _fresh_activity()

    # Deterministic clock: CallNextHookEx advances it by 5 ms, so the
    # recorded sample must include the hook-chain time (not just our own
    # bookkeeping around it).
    clock = [100.0]
    monkeypatch.setattr(computer_mod.time, "perf_counter", lambda: clock[0])

    class FakeUser32:
        def CallNextHookEx(self, *a):
            clock[0] += 0.005
            return 0

    user32 = FakeUser32()
    called = []
    original = act._record_latency

    def spying_record(elapsed_ms):
        called.append(elapsed_ms)
        original(elapsed_ms)

    monkeypatch.setattr(act, "_record_latency", spying_record)
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
    assert len(called) == 1, called
    assert called[0] == pytest.approx(5.0), called


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


@pytest.mark.asyncio
async def test_branch_lookup_discards_stderr(monkeypatch, tmp_path):
    """A local branch literally named HEAD makes `rev-parse --abbrev-ref HEAD`
    print an ambiguity warning on stderr while still exiting 0. Merging
    stderr into stdout would store 'warning: ...
HEAD' as the branch, so
    the branch lookup must discard stderr (other readouts keep merged)."""
    captured = {}

    async def spy(root, *args, merge_stderr=True):
        captured["merge_stderr"] = merge_stderr
        return 0, "HEAD"

    monkeypatch.setattr(gitinfo, "_run_git", spy)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/HEAD" + chr(10))
    try:
        branch = await gitinfo.current_git_branch(tmp_path)
    finally:
        gitinfo._cache.pop(str(tmp_path), None)
    assert captured["merge_stderr"] is False
    assert branch == "HEAD"


@pytest.mark.asyncio
async def test_run_git_can_discard_stderr(tmp_path):
    """merge_stderr=False must route stderr to DEVNULL so ambiguity
    warnings never reach the parsed output; True keeps the merged default."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main" + chr(10))
    # `verbose` is not a git command: its usage text goes to stderr.
    rc, out = await gitinfo._run_git(tmp_path, "verbose")
    assert rc != 0 and "not a git command" in out.lower()
    rc, out = await gitinfo._run_git(tmp_path, "verbose", merge_stderr=False)
    assert rc != 0 and out == ""
