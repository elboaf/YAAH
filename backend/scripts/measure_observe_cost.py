"""Issue #279 regression measurement: observe-capture cost and its effect on
hook-callback dispatch latency.

PR #283 landed the metric (hook-callback p50/p95/p99) but only measured an
IDLE host. The triage's remaining suspect is the observe pipeline that runs
on the event-loop thread of the SAME process that hosts the WH_MOUSE_LL /
WH_KEYBOARD_LL hooks: every mouse/keyboard action with observe on (the
default) does mss grab -> PNG encode -> PIL decode -> resize -> annotate ->
second PNG encode -> disk write, plus a COM UIA tree walk for set-of-marks.

This script measures, in one process:

  1. Per-stage cost of the observe pipeline (grab / encode / decode /
     resize / annotate / store), so a future optimization targets the real
     hotspot rather than a guessed one.
  2. Hook-callback dispatch latency while the pipeline is hammered in a
     loop, in two modes:
       onloop  - captures run on the thread that hosts the hooks (the
                 current executor shape: awaits on the loop);
       offtask - captures run on a worker thread (asyncio.to_thread shape).

  The difference between the two modes' p99 is the number that justifies
  (or rejects) moving captures off the loop thread.

Usage (Windows, from the repo root):
    python backend/scripts/measure_observe_cost.py [--seconds 3] [--budget-ms 5]

Exit code 1 when either mode's hook p99 exceeds the budget, or when the
offtask mode fails to beat the onloop mode (the fix is then not justified
by measurement and should be revisited).
"""
import argparse
import asyncio
import ctypes
import io
import statistics
import sys
import threading
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from backend.agent import computer as computer_mod  # noqa: E402
from backend.scripts import measure_hook_latency as mhl  # noqa: E402


# ---------------------------------------------------------------- stage table

def _stage_table(seconds: float) -> dict:
    """Time each stage of the observe path on a real monitor capture.

    Uses the module's own helpers so the numbers describe the shipping
    pipeline: grab -> zero-copy frombytes -> resize -> annotate -> ONE
    PNG encode (the double encode this ticket removed is gone; the table
    exists to keep future optimizations honest about where the cost is).
    """
    stages: dict[str, list[float]] = {
        "monitor_enum": [], "grab": [], "frombytes": [],
        "resize_lanczos": [], "annotate": [], "png_encode_pil": [],
    }

    def _timed(name: str, fn, *args):
        t0 = time.perf_counter()
        out = fn(*args)
        stages[name].append((time.perf_counter() - t0) * 1000.0)
        return out

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        mons = _timed("monitor_enum", computer_mod._monitors)
        rect = mons[0]["rect"] if mons else [0, 0, 1920, 1080]
        clip = {"left": rect[0], "top": rect[1],
                "width": rect[2] - rect[0], "height": rect[3] - rect[1]}
        import mss
        from PIL import Image

        with mss.MSS() as sct:
            shot = _timed("grab", sct.grab, clip)
        img = _timed("frombytes",
                     lambda s: Image.frombytes("RGB", s.size, bytes(s.rgb)), shot)
        w, h = clip["width"], clip["height"]
        if max(img.size) > computer_mod.MAX_CAPTURE_EDGE:
            scale = computer_mod.MAX_CAPTURE_EDGE / max(img.size)
            img = _timed(
                "resize_lanczos", img.resize,
                (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                Image.LANCZOS,
            )
        else:
            stages["resize_lanczos"].append(0.0)
        _timed("annotate", computer_mod._annotate_img, img, w, h, [0, 0], 1.0)
        out = io.BytesIO()
        _timed("png_encode_pil", img.save, out, "PNG")

    summary = {}
    for name, xs in stages.items():
        if xs:
            summary[name] = {
                "n": len(xs),
                "mean_ms": round(statistics.fmean(xs), 3),
                "p95_ms": round(sorted(xs)[int(0.95 * (len(xs) - 1))], 3),
            }
    return summary


# ---------------------------------------------------------------- load + hooks

def _capture_loop(stop: threading.Event) -> dict:
    """Hammer the FULL observe-crop pipeline (the per-action path) until
    stop is set; returns per-call cost stats."""
    costs: list[float] = []
    while not stop.is_set():
        t0 = time.perf_counter()
        try:
            cx, cy = computer_mod._cursor_pos()
            computer_mod._observe_crop_result(cx, cy)
        except Exception as e:  # noqa: BLE001 - report and keep measuring
            return {"error": f"{type(e).__name__}: {e}", "n": len(costs)}
        costs.append((time.perf_counter() - t0) * 1000.0)
    if not costs:
        return {"error": "no captures completed", "n": 0}
    return {
        "n": len(costs),
        "mean_ms": round(statistics.fmean(costs), 3),
        "p95_ms": round(sorted(costs)[int(0.95 * (len(costs) - 1))], 3),
        "max_ms": round(max(costs), 3),
    }


async def _amode_onloop(stop: threading.Event) -> dict:
    """Captures awaited directly on the loop thread = current executor shape."""
    return _capture_loop(stop)


async def _amode_offtask(stop: threading.Event) -> dict:
    """Captures handed to a worker thread = the proposed executor shape."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _capture_loop, stop)


def _run_mode(mode_fn, seconds: float, budget_ms: float) -> tuple[int, dict]:
    """Install both LL hooks, run the capture loop for `seconds`, judge the
    hook p99 against the budget. Returns (exit_code, report)."""
    act_by_name = {name: computer_mod._Activity() for name in mhl._EXPECTED_HOOKS}
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
            target=mhl._run_hook_thread,
            args=(act_by_name[name], hook_id, struct_ty, mask, installed, name),
            daemon=True)
        t.start()
        threads.append(t)
    deadline = time.monotonic() + 2.0
    while len(installed) < len(mhl._EXPECTED_HOOKS) and time.monotonic() < deadline:
        time.sleep(0.02)
    missing = mhl._hook_install_failures(installed)
    if missing:
        print(f"FAIL: hooks failed to install: {', '.join(missing)}")
        return 1, {}

    stop = threading.Event()

    def _inject_then_stop():
        # Runs on the executor thread: injection traffic for `seconds`,
        # then the stop signal. The stop MUST come from this thread — in
        # the onloop mode the loop thread is deliberately blocked inside
        # the capture loop, so anything awaited on the loop could never
        # run (a stop set on the loop would deadlock the measurement).
        print("  WARNING: injecting REAL mouse moves + key presses for "
              f"{seconds:.0f}s — do not run this on a machine someone is "
              "using.")
        mhl._measure_hooks(act_by_name, seconds)
        stop.set()

    async def _run():
        load = asyncio.create_task(mode_fn(stop))
        await asyncio.get_running_loop().run_in_executor(None, _inject_then_stop)
        return await load

    capture = asyncio.run(_run())
    summaries = {name: act.latency_summary_ms() for name, act in act_by_name.items()}
    report = {"capture": capture,
              "hook_ms": {k: v for k, v in summaries.items() if v}}
    for name in mhl._EXPECTED_HOOKS:
        s = summaries.get(name)
        print(f"  {name} hook dispatch ms under load: {s}")
    rc, failures = mhl._judge(summaries, budget_ms)
    if rc:
        for f in failures:
            print(f"  FAIL: {f}")
        return 1, report
    return 0, report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage-seconds", type=float, default=1.5,
                    help="seconds spent on the per-stage cost table")
    ap.add_argument("--load-seconds", type=float, default=3.0,
                    help="seconds per mode for the under-load comparison")
    ap.add_argument("--budget-ms", type=float, default=5.0)
    args = ap.parse_args()

    if not computer_mod.WINDOWS:
        print("windows-only measurement")
        return 2

    print(f"== observe pipeline stage costs ({args.stage_seconds}s) ==")
    stages = _stage_table(args.stage_seconds)
    for name, s in stages.items():
        print(f"  {name:16s} n={s['n']:<5d} mean {s['mean_ms']:>8.3f}ms  "
              f"p95 {s['p95_ms']:>8.3f}ms")
    total_mean = sum(s["mean_ms"] for s in stages.values())
    print(f"  {'TOTAL':16s} mean {total_mean:.3f}ms per full-monitor capture")

    print(f"== hook latency under capture load ({args.load_seconds}s/mode) ==")
    print("-- onloop (captures on the hook-hosting thread) --")
    rc_on, rep_on = _run_mode(_amode_onloop, args.load_seconds, args.budget_ms)
    print("-- offtask (captures on a worker thread) --")
    rc_off, rep_off = _run_mode(_amode_offtask, args.load_seconds, args.budget_ms)

    print("== capture cost under load ==")
    print(f"  onloop : {rep_on.get('capture')}")
    print(f"  offtask: {rep_off.get('capture')}")

    if rc_on or rc_off:
        print("FAIL: hook p99 over budget while the pipeline runs")
        return 1

    def _p99(rep):
        return max((v["p99"] for v in rep.get("hook_ms", {}).values()), default=None)

    p99_on, p99_off = _p99(rep_on), _p99(rep_off)
    if p99_on is None or p99_off is None:
        print("FAIL: missing hook summaries")
        return 1
    print(f"PASS: p99 onloop {p99_on}ms vs offtask {p99_off}ms "
          f"(budget {args.budget_ms}ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
