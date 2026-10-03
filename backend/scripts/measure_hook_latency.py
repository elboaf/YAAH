"""Issue #279 regression measurement: hook-callback dispatch latency.

Installs the real WH_MOUSE_LL / WH_KEYBOARD_LL hooks for a few seconds,
wiggles synthetic (injected — so they are not recorded as activity, but the
hook chain still dispatches them, and the timing wrapper measures the full
dispatch) mouse moves and key presses, then reports per-hook p50/p95/p99
and exits non-zero when a budget is exceeded or a hook is unverified.

Usage (Windows, from the repo root):
    python backend/scripts/measure_hook_latency.py [--seconds 5] [--budget-ms 5]

Note on injection: SendInput-generated input carries the *_INJECTED flag,
and the callback's activity record skips it — but the timing wrapper still
measures the full dispatch, which is exactly the metric cursor smoothness
depends on. So the script injects input with SendInput to generate real
hook traffic without polluting the user-activity timestamp.

Each hook is measured against its own _Activity and judged on its own
samples: an installed keyboard hook that never sees a keyboard event must
not hide behind healthy mouse numbers.

The --budget-ms threshold is the pass/fail line for CI-style regression
runs: any hook's p99 above it means hook dispatch is being delayed (cursor
stutter territory) and the script exits 1.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from backend.agent import computer as computer_mod  # noqa: E402

MOUSEEVENTF_MOVE = 0x0001
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_SHIFT = 0x10

_EXPECTED_HOOKS = ("mouse", "keyboard")


def _inject_move(dx: int, dy: int) -> None:
    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                    ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]

    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", _MOUSEINPUT)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wt.DWORD), ("u", _U)]

    inp = _INPUT(type=INPUT_MOUSE)
    inp.mi = _MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _inject_key(vk: int, release: bool = False) -> None:
    # The INPUT union must be sized by its LARGEST member (MOUSEINPUT, 40
    # bytes total on x64): a keyboard-only union makes cbSize too small and
    # SendInput rejects the call with an invalid-parameter error.
    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                    ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD),
                    ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]

    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wt.DWORD), ("u", _U)]

    flags = KEYEVENTF_KEYUP if release else 0
    inp = _INPUT(type=INPUT_KEYBOARD)
    inp.ki = _KEYBDINPUT(vk, 0, flags, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _hook_install_failures(installed: dict[str, bool]) -> list[str]:
    """Names of the expected hooks that did not report a successful install,
    sorted. A key that is absent (thread never reported: crashed, or still
    starting) or False is a failure — a partial install silently narrows
    what the run measures, so callers must treat any failure as failed."""
    return sorted(
        name for name in _EXPECTED_HOOKS if not installed.get(name, False))


def _run_hook_thread(activity, hook_id, struct_ty, injected_mask,
                     installed, name):
    """Mirror of _Activity._hook_thread but bound to a private _Activity.
    Installs the hook, records the outcome in `installed[name]` (True on
    success, False on failure — always reported, so the caller can wait for
    both outcomes), then pumps messages on this thread (a requirement for
    LL hooks)."""
    user32 = ctypes.windll.user32
    hookproc_ty = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, ctypes.c_ssize_t, ctypes.c_ssize_t)
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, hookproc_ty, ctypes.c_void_p, ctypes.c_uint]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.CallNextHookEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t, ctypes.c_ssize_t]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    proc = hookproc_ty(computer_mod._make_timed_callback(
        activity, struct_ty, injected_mask, lambda: user32))
    hook = user32.SetWindowsHookExW(hook_id, proc, None, 0)
    installed[name] = bool(hook)
    if not hook:
        print(f"hook {hook_id} ({name}) failed to install")
        return
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    user32.UnhookWindowsHookEx(hook)


def _measure_hooks(activities, seconds: float) -> dict:
    """Inject mouse and keyboard traffic and return each hook's latency
    summary (None when a hook recorded no samples).

    NOTE: this MOVES THE REAL CURSOR / PRESSES REAL KEYS for `seconds` —
    run it when that is tolerable, or inside a disposable VM, or use
    --passive (which never injects)."""
    injectors = [
        ("mouse", lambda i: _inject_move((i % 3) - 1, (i % 5) - 2)),
        ("keyboard", lambda i: (
            _inject_key(VK_SHIFT, release=(i % 2 == 1)))),
    ]
    end = time.monotonic() + seconds
    i = 0
    while time.monotonic() < end:
        for _, inject in injectors:
            inject(i)
        i += 1
        time.sleep(0.002)
    return {name: act.latency_summary_ms()
            for name, act in activities.items()}


def _measure_passive(activities, seconds: float) -> dict:
    """Collect hook-callback latency from REAL user input only — nothing
    is injected and the cursor is never touched. Useful on a machine
    someone is actively using; needs at least a few real mouse/keyboard
    events during the window to produce samples."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(0.1)
    return {name: act.latency_summary_ms()
            for name, act in activities.items()}


def _judge(summaries: dict, budget_ms: float):
    """Per-hook pass/fail. Returns (exit_code, failure_messages): every
    expected hook must have samples AND a p99 within budget — a hook with
    no samples (e.g. an installed keyboard hook that got no key events)
    fails even when the other hook is healthy."""
    failures = []
    for name in _EXPECTED_HOOKS:
        s = summaries.get(name)
        if s is None:
            failures.append(f"{name}: no latency samples — hook did not dispatch")
        elif s["p99"] > budget_ms:
            failures.append(
                f"{name}: p99 {s['p99']}ms exceeds budget {budget_ms}ms")
    if failures:
        return 1, failures
    return 0, []


def _run_measurement(seconds: float, budget_ms: float, passive: bool = False):
    """Install both hooks, measure, judge. Returns (exit_code, summaries);
    prints the human-readable report. passive=True never injects input:
    samples come from whatever the user really does during the window."""
    act_by_name = {name: computer_mod._Activity() for name in _EXPECTED_HOOKS}
    # LL hooks must install AND pump messages on the same thread, so each
    # hook gets a daemon thread that reports its install outcome back.
    installed: dict[str, bool] = {}
    hook_specs = {
        "mouse": (computer_mod.WH_MOUSE_LL, computer_mod._MSLLHOOKSTRUCT,
                  computer_mod._LLMHF_INJECTED),
        "keyboard": (computer_mod.WH_KEYBOARD_LL, computer_mod._KBDLLHOOKSTRUCT,
                     computer_mod._LLKHF_INJECTED),
    }
    threads = []
    for name, (hook_id, struct_ty, mask) in hook_specs.items():
        t = threading.Thread(
            target=_run_hook_thread,
            args=(act_by_name[name], hook_id, struct_ty, mask,
                  installed, name),
            daemon=True)
        t.start()
        threads.append(t)
    # Wait until every hook thread has REPORTED an outcome (not merely for
    # a fixed sleep): an absent key must not be read as success.
    deadline = time.monotonic() + 2.0
    while len(installed) < len(_EXPECTED_HOOKS) and time.monotonic() < deadline:
        time.sleep(0.02)
    missing = _hook_install_failures(installed)
    if missing:
        print(f"FAIL: hooks failed to install: {', '.join(missing)}")
        return 1, None

    if passive:
        print(f"PASSIVE measurement for {seconds:.0f}s: move your mouse / type "
              f"normally — nothing will be injected.")
        summaries = _measure_passive(act_by_name, seconds)
    else:
        print(f"WARNING: injecting synthetic mouse moves + key presses for "
              f"{seconds:.0f}s — the REAL cursor will move. Abort now if "
              f"that is not okay (Ctrl+C).")
        time.sleep(1.0)
        summaries = _measure_hooks(act_by_name, seconds)
    for name in _EXPECTED_HOOKS:
        s = summaries.get(name)
        print(f"{name} samples: {len(act_by_name[name]._latencies)}")
        print(f"{name} hook dispatch latency ms: {s}")
    if passive and all(summaries.get(n) is None for n in _EXPECTED_HOOKS):
        print("FAIL: no real input observed in the window — move the mouse "
              "or type during a passive run")
        return 1, None
    rc, failures = _judge(summaries, budget_ms)
    if rc:
        # Passive runs judge only what real input exercised; a hook with no
        # real events is 'not measured', not 'failed'.
        if passive:
            failures = [f for f in failures if "no latency samples" not in f]
            if not failures:
                print("PASS: sampled hooks within budget (input-starved "
                      "hooks not measured)")
                return 0, summaries
        for f in failures:
            print(f"FAIL: {f}")
        return rc, failures
    print(f"PASS: every hook p99 within {budget_ms}ms budget")
    return 0, summaries


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--budget-ms", type=float, default=5.0)
    ap.add_argument("--passive", action="store_true",
                    help="never inject input; sample real user activity only")
    args = ap.parse_args()

    if not computer_mod.WINDOWS:
        print("windows-only measurement")
        return 2

    rc, _ = _run_measurement(args.seconds, args.budget_ms, passive=args.passive)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
