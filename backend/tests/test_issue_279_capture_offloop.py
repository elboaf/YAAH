"""Issue #279: observe-capture work must leave the event-loop thread.

The executors run on the event loop of the process that hosts the
WH_MOUSE_LL / WH_KEYBOARD_LL hooks; a capture is a 20-180ms CPU burst
(grab + resize + PNG encode) and that burst ON the hook-hosting thread is
the run-correlated cursor stutter. These tests pin:

- every capture await in the async executors runs on a worker thread, not
  on the calling (loop) thread;
- the capture helpers encode ONCE (the old path compressed to PNG twice);
- the BGRA->RGB handoff produces correctly-colored pixels;
- failures still surface as {"error": ...} / observe_error dicts, never
  raises, across the thread boundary.
"""
import asyncio
import io
import threading

import pytest

from backend.agent import computer as computer_mod

# The capture seam imports mss/PIL lazily; CI runs the backend suite on
# Linux where those are win32-marked deps and absent.
pytest.importorskip("mss")
pytest.importorskip("PIL")


# ---------------------------------------------------------------- fakes

class _FakeShot:
    """mss ScreenShot stand-in: rgb bytes + size."""

    def __init__(self, width: int, height: int, rgb: bytes):
        self.rgb = rgb
        self.raw = rgb  # the fakes skip the BGRA shuffle; see the color test
        self.size = (width, height)
        self.width = width
        self.height = height


class _FakeMSS:
    """mss.MSS stand-in: context manager + grab returning a fixed shot."""

    shots: list[_FakeShot] = []
    instances = 0
    monitors = [{}, {"left": 0, "top": 0, "width": 400, "height": 400}]

    def __init__(self, *a, **k):
        _FakeMSS.instances += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def grab(self, monitor):
        return self.shots[0]


@pytest.fixture()
def fake_mss(monkeypatch):
    import mss

    # The helpers call the mss.mss() FACTORY (works across mss versions,
    # unlike the MSS class); patch it for the same reason.
    monkeypatch.setattr(mss, "mss", lambda *a, **k: _FakeMSS(*a, **k))
    _FakeMSS.instances = 0
    return _FakeMSS


def _rgb_solid(width: int, height: int, r: int, g: int, b: int) -> bytes:
    return bytes([r, g, b]) * (width * height)


def _capture_thread_idents(monkeypatch):
    """Proxy both capture seams (_capture_screen full-monitor and
    _capture_clip regions/crops) so they record the thread ident they
    RUN on."""
    idents: list[int] = []

    for name in ("_capture_screen", "_capture_clip"):
        orig = getattr(computer_mod, name)

        def proxy(*args, _orig=orig, **kwargs):
            idents.append(threading.get_ident())
            return _orig(*args, **kwargs)

        monkeypatch.setattr(computer_mod, name, proxy)
    return idents


def _assert_off_loop(idents: list[int], main_ident: int, what: str):
    assert idents, f"{what}: capture work never ran (seam removed?)"
    off = [i for i in idents if i != main_ident]
    assert off, f"{what}: capture work ran ON the calling (loop) thread"


# ---------------------------------------------------------------- off-thread

def test_screenshot_runs_capture_off_loop_thread(fake_mss, monkeypatch):
    """The screenshot executor must hand capture work to a worker thread:
    on the hook-hosting loop thread it IS the cursor stutter."""
    _FakeMSS.shots = [_FakeShot(400, 400, _rgb_solid(400, 400, 10, 20, 30))]
    idents = _capture_thread_idents(monkeypatch)
    monkeypatch.setattr(computer_mod, "_store_png",
                        lambda *a, **k: {"image": "screenshots/f.png",
                                         "monitor": 1, "size": [400, 400],
                                         "origin": [0, 0]})
    main_ident = threading.get_ident()

    res = asyncio.run(computer_mod.screenshot(monitor=1))

    assert "error" not in res, res
    _assert_off_loop(idents, main_ident, "screenshot")


def test_observe_crop_runs_capture_off_loop_thread(fake_mss, monkeypatch):
    """The observe crop (per mouse/keyboard action) runs off-thread too."""
    _FakeMSS.shots = [_FakeShot(400, 400, _rgb_solid(400, 400, 0, 0, 0))]
    idents = _capture_thread_idents(monkeypatch)
    monkeypatch.setattr(computer_mod, "_cursor_pos", lambda: (100, 100))
    monkeypatch.setattr(computer_mod, "_monitor_for_point", lambda x, y: 1)
    monkeypatch.setattr(computer_mod, "_monitor_rect", lambda m: [0, 0, 1920, 1080])
    monkeypatch.setattr(computer_mod, "_store_png",
                        lambda *a, **k: {"image": "screenshots/x.png",
                                         "monitor": 1, "size": [400, 400],
                                         "origin": [0, 0]})
    main_ident = threading.get_ident()

    res = asyncio.run(computer_mod.mouse_move(x=100, y=100, monitor=1, observe=True))

    assert res.get("ok") is True
    assert res.get("image") == "screenshots/x.png"
    _assert_off_loop(idents, main_ident, "observe crop")


def test_som_overlay_runs_off_loop_thread(fake_mss, monkeypatch):
    """elements=True: the UIA COM walk + overlay re-encode are the longest
    bursts — they must not run on the loop thread either."""
    _FakeMSS.shots = [_FakeShot(400, 400, _rgb_solid(400, 400, 0, 0, 0))]
    idents: list[int] = []

    def fake_som(rel, sw, sh, mon, hwnd, limit=30):
        idents.append(threading.get_ident())
        return {
            "elements": [
                {"id": 0, "name": "Go", "type": "ButtonControl",
                 "center": [100, 100]},
            ],
            "truncated": False,
        }

    monkeypatch.setattr(computer_mod, "_monitor_for_window", lambda hwnd: 1)
    monkeypatch.setattr(computer_mod, "_som_overlay", fake_som)
    monkeypatch.setattr(computer_mod, "_store_png",
                        lambda *a, **k: {"image": "screenshots/som.png",
                                         "monitor": 1, "size": [400, 400],
                                         "origin": [0, 0]})
    main_ident = threading.get_ident()

    res = asyncio.run(computer_mod.screenshot(hwnd=7, elements=True))

    assert res.get("hwnd") == 7
    assert res["elements"][0]["name"] == "Go"
    _assert_off_loop(idents, main_ident, "uia/som overlay")


# ---------------------------------------------------------------- single encode

def test_capture_helpers_return_raw_rgb_no_encode(fake_mss):
    """The capture seams return UNCOMPRESSED RGB rows (the single PNG
    encode belongs to _store_png), with size attached."""
    red = _rgb_solid(64, 32, 255, 0, 0)
    _FakeMSS.shots = [_FakeShot(64, 32, red)]
    raw, w, h = computer_mod._capture_screen(1)
    assert (w, h) == (64, 32)
    assert raw == red  # untouched rows, zero encodes on this path

    _FakeMSS.shots = [_FakeShot(100, 80, _rgb_solid(100, 80, 5, 6, 7))]
    raw, w, h = computer_mod._capture_clip(
        {"left": 0, "top": 0, "width": 100, "height": 80})
    assert (w, h) == (100, 80)
    assert raw == _rgb_solid(100, 80, 5, 6, 7)


def test_store_png_encodes_raw_rgb_with_correct_channels(fake_mss):
    """The pipeline's ONE encode lives in _store_png; channel order must
    survive raw-rows -> frombytes (a swap turns a red frame blue)."""
    from backend.agent import imagedata

    stores: list[bytes] = []

    def fake_save(raw, ext, subdir=""):
        stores.append(raw)
        return f"{subdir}/fake.png"

    import unittest.mock as mock

    with mock.patch.object(imagedata, "save_bytes", fake_save):
        res = computer_mod._store_png(
            _rgb_solid(64, 32, 255, 0, 0), 64, 32, 1, [0, 0])
    assert res["size"] == [64, 32]
    assert len(stores) == 1

    from PIL import Image

    img = Image.open(io.BytesIO(stores[0])).convert("RGB")
    assert img.getpixel((10, 10)) == (255, 0, 0)


# ---------------------------------------------------------------- failure surface

def test_screenshot_failure_is_error_dict_not_raise(monkeypatch):
    """Off-loading must not change the never-raise contract: a failing
    capture still returns {"error": ...}."""
    import mss

    class _Boom:
        monitors = [{}, {"left": 0, "top": 0, "width": 400, "height": 400}]

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def grab(self, monitor):
            raise OSError("display gone")

    monkeypatch.setattr(mss, "mss", lambda *a, **k: _Boom())
    res = asyncio.run(computer_mod.screenshot(monitor=1))
    assert "error" in res
    assert "display gone" in res["error"]


def test_capture_failure_does_not_mask_move_feedback(fake_mss, monkeypatch):
    """A capture failure inside observe must not mask the move's own
    feedback (pre-existing contract, now across the thread boundary)."""
    _FakeMSS.shots = [_FakeShot(400, 400, b"\x00" * (400 * 400 * 3))]
    monkeypatch.setattr(computer_mod, "_cursor_pos", lambda: (100, 100))
    monkeypatch.setattr(computer_mod, "_monitor_for_point", lambda x, y: 1)
    monkeypatch.setattr(computer_mod, "_monitor_rect", lambda m: [0, 0, 1920, 1080])

    def _boom(*a, **k):
        raise RuntimeError("store failed")

    monkeypatch.setattr(computer_mod, "_store_png", _boom)
    res = asyncio.run(computer_mod.mouse_move(x=100, y=100, monitor=1, observe=True))

    assert res["ok"] is True
    assert res["cursor"] == [100, 100]
    assert "observe_error" in res
