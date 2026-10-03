"""Issue #279 regression measurement: hook-callback dispatch latency.

Installs the real WH_MOUSE_LL / WH_KEYBOARD_LL hooks for a few seconds,
wiggles a synthetic (injected — so it is not recorded as activity, but the
hook chain still dispatches it... actually injected events ARE dispatched
through the hook but flagged injected, which our callback still times)
— see note below — then reports p50/p95/p99 and exits non-zero when the
budget is exceeded.

Usage (Windows, from the repo root):
    python backend/scripts/measure_hook_latency.py [--seconds 5] [--budget-ms 5]

Note on injection: SendInput-generated moves carry LLMHF_INJECTED, and the
callback's activity record skips them — but the timing wrapper still measures
the full dispatch, which is exactly the metric cursor smoothness depends on.
So the script injects mouse moves with SendInput to generate real hook
traffic without polluting the user-activity timestamp.

The --budget-ms threshold is the pass/fail line for CI-style regression
runs: p99 above it means hook dispatch is being delayed (cursor stutter
territory) and the script exits 1.
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


def _hook_install_failures(installed: dict[str, bool]) -> list[str]:
    """Names of hooks that were expected but failed to install, sorted.
    A partial install (e.g. only the mouse hook) silently narrows what the
    run measures, so callers must treat any failure as a failed run."""
    return sorted(name for name, ok in installed.items() if not ok)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--budget-ms", type=float, default=5.0)
    args = ap.parse_args()

    if not computer_mod.WINDOWS:
        print("windows-only measurement")
        return 2

    act = computer_mod._Activity()
    # A module-level instance is what production installs; use a private one
    # per run so repeated invocations measure only their own window.
    # LL hooks must install AND pump messages on the same thread, so each
    # hook gets a daemon thread that reports its install result back.
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
            args=(act, hook_id, struct_ty, mask, installed, name),
            daemon=True)
        t.start()
        threads.append(t)
    time.sleep(0.2)  # let the hooks install
    missing = _hook_install_failures(installed)
    if missing:
        print(f"FAIL: hooks failed to install: {', '.join(missing)}")
        return 1

    user32 = ctypes.windll.user32
    end = time.monotonic() + args.seconds
    i = 0
    while time.monotonic() < end:
        _inject_move((i % 3) - 1, (i % 5) - 2)
        i += 1
        time.sleep(0.002)

    summary = act.latency_summary_ms()
    print(f"samples: {len(act._latencies)}")
    print(f"hook dispatch latency ms: {summary}")
    if summary is None:
        print("FAIL: no latency samples recorded — hooks did not dispatch")
        return 1
    if summary["p99"] > args.budget_ms:
        print(f"FAIL: p99 {summary['p99']}ms exceeds budget {args.budget_ms}ms")
        return 1
    print(f"PASS: p99 within {args.budget_ms}ms budget")
    return 0


def _run_hook_thread(activity, hook_id, struct_ty, injected_mask,
                     installed, name):
    """Mirror of _Activity._hook_thread but bound to a private _Activity.
    Installs the hook, records the outcome in `installed[name]` (True only
    on success — the key is simply absent on failure), then pumps messages
    on this thread (a requirement for LL hooks)."""
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
    if not hook:
        print(f"hook {hook_id} ({name}) failed to install")
        return
    installed[name] = True
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    user32.UnhookWindowsHookEx(hook)


if __name__ == "__main__":
    raise SystemExit(main())
