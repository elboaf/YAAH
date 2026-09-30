"""Lifecycle tests for the optional Windows Sandbox preview manager."""

import threading

from backend.agent import sandbox_preview as preview


def test_start_preview_skips_non_windows_and_headless(monkeypatch):
    monkeypatch.setattr(preview, "_manager", None)
    monkeypatch.setattr(preview.os, "name", "posix")
    assert preview.start_preview() is False
    assert preview._manager is None

    monkeypatch.setattr(preview.os, "name", "nt")
    monkeypatch.setenv("YAAH_HEADLESS", "1")
    assert preview.start_preview() is False
    assert preview._manager is None


def test_start_preview_is_idempotent_and_stop_releases_manager(monkeypatch):
    monkeypatch.setattr(preview.os, "name", "nt")
    monkeypatch.delenv("YAAH_HEADLESS", raising=False)
    monkeypatch.setattr(preview, "_manager", None)
    events = []

    class FakeManager:
        running = True
        stopping = False

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")
            self.running = False
            self.stopping = True

    monkeypatch.setattr(preview, "_PreviewManager", FakeManager)
    assert preview.start_preview() is True
    first_manager = preview._manager
    assert preview.start_preview() is True
    assert preview._manager is first_manager
    assert events == ["start"]

    preview.stop_preview()
    assert preview._manager is None
    assert events == ["start", "stop"]


def test_main_window_selection_prefers_largest_candidate():
    # EnumWindows may report an auxiliary/title-bar HWND before the full client.
    candidates = [(320 * 36, 0x101), (1280 * 720, 0x202), (900 * 600, 0x303)]
    assert preview._select_main_window(candidates) == 0x202


def test_main_window_selection_handles_no_candidates():
    assert preview._select_main_window([]) == 0


def test_manager_stop_signals_and_joins_preview_thread():
    entered = threading.Event()
    release = threading.Event()

    manager = preview._PreviewManager()

    def wait_until_stopped():
        entered.set()
        manager._stop.wait()
        release.set()

    manager._run_native = wait_until_stopped
    manager.start()
    assert entered.wait(timeout=1)
    manager.stop()
    assert release.is_set()
    assert not manager.running


# ---- #116: moveable/resizeable preview + pin-to-yaah ----


def test_fit_thumbnail_rect_letterboxes_inside_client():
    # 16:9 source into a 4:3-ish client -> width-limited, vertically centered.
    left, top, w, h = preview.fit_thumbnail_rect(200, 150, 1600, 900)
    assert (w, h) == (200, 112)
    assert (left, top) == (0, 19)


def test_fit_thumbnail_rect_falls_back_when_source_unknown():
    left, top, w, h = preview.fit_thumbnail_rect(420, 236, 0, 0)
    assert (left, top, w, h) == (0, 0, 420, 236)


def test_find_yaah_window_uses_title_prefix(monkeypatch):
    class FakeUser32:
        def __init__(self):
            self.calls = []

        def FindWindowW(self, cls, title):
            self.calls.append((cls, title))
            return 0x42

    user32 = FakeUser32()
    assert preview._find_yaah_window(user32) == 0x42
    assert user32.calls == [(None, "YAAH")]


def test_find_yaah_window_returns_zero_when_absent():
    class FakeUser32:
        def FindWindowW(self, cls, title):
            return 0

    assert preview._find_yaah_window(FakeUser32()) == 0


def test_find_yaah_window_returns_zero_when_ctypes_returns_none():
    # Regression (#131): with restype=HWND, FindWindowW's NULL comes back as
    # None, and int(None) crashed the preview thread before it could show
    # the window ("sandbox live preview failed" in the backend log).
    class FakeUser32:
        def FindWindowW(self, cls, title):
            return None

    assert preview._find_yaah_window(FakeUser32()) == 0


# ---- #122: eve-o-preview-style gestures ----


def test_preview_min_size_defaults():
    assert (preview._PREVIEW_MIN_WIDTH, preview._PREVIEW_MIN_HEIGHT) == (100, 80)


# ---- confinement & always-on follow (user round 3) ----


def test_clamp_to_rect_keeps_window_inside_bounds():
    # Whole-rect clamp: a window dragged past yaah's client edge is pulled
    # back so it is fully inside the bounds.
    assert preview.clamp_to_rect(
        (900, 550, 1000, 650), (0, 0, 800, 600)
    ) == (700, 500, 800, 600)
    # Already inside: unchanged.
    assert preview.clamp_to_rect(
        (10, 10, 200, 150), (0, 0, 800, 600)
    ) == (10, 10, 200, 150)


def test_clamp_to_rect_shrinks_oversized_window():
    # Yaah smaller than the preview: the preview shrinks to fit (top-left
    # stays anchored, size floors at the preview minimum).
    assert preview.clamp_to_rect(
        (0, 0, 500, 400), (0, 0, 200, 150)
    ) == (0, 0, 200, 150)


def test_start_position_insets_from_client_top_left():
    # Fresh start: just inside yaah's client top-left with an inset so
    # yaah's own toolbar stays visible.
    assert preview.start_position((100, 50, 1100, 750)) == (112, 62)
    assert preview.start_position((0, 0, 800, 600)) == (12, 12)


def test_resize_target_caps_at_bounds():
    # The preview can never exceed yaah's client dimensions: the old 640x400
    # ceiling is replaced by the confinement bounds (ratio stays exact).
    rect = preview.resize_target(
        start_rect=(0, 0, 200, 112),
        start=(0, 0),
        current=(3000, 3000),
        aspect=16 / 9,
        bounds=(0, 0, 800, 450),
    )
    assert (rect[2] - rect[0], rect[3] - rect[1]) == (800, 450)



# ---- feedback round 2 (user reports): pairing, uniform hit, unclamped ----


def test_button_up_message_pairs_with_its_down():
    # finish_gesture is gated on drag["button"] == message at WM_*BUTTONUP;
    # the stored anchor must therefore be the UP constant, not the DOWN one
    # (the old DOWN store never matched, so the gesture session leaked and
    # poisoned the next click — reported as alternating dead clicks).
    assert preview._GESTURE_BUTTON_OF[preview.WM_LBUTTONDOWN] == preview.WM_LBUTTONUP
    assert preview._GESTURE_BUTTON_OF[preview.WM_RBUTTONDOWN] == preview.WM_RBUTTONUP


def test_gesture_hit_is_uniform_across_the_window():
    # Right-drag must land anywhere on the preview, not just near its center:
    # WM_NCHITTEST always returns HTCLIENT for a borderless gesture overlay.
    assert preview.gesture_hit_test(0, 0) == preview.HTCLIENT
    assert preview.gesture_hit_test(640, 400) == preview.HTCLIENT
    assert preview.gesture_hit_test(320, 200) == preview.HTCLIENT


def test_resize_target_has_no_size_ceiling():
    # The preview may be sized however the user wants: the max clamp is gone
    # (min stays — a 1px sliver is never a useful preview).
    big = preview.resize_target(
        start_rect=(0, 0, 420, 236),
        start=(0, 0),
        current=(3000, 3000),
        aspect=16 / 9,
    )
    assert big[2] - big[0] > 2000
    assert big[3] - big[1] > 1000
    assert abs((big[2] - big[0]) / (big[3] - big[1]) - 16 / 9) < 0.02
    # Freeform too.
    free = preview.resize_target(
        start_rect=(0, 0, 100, 100), start=(0, 0), current=(2500, 2500), aspect=None
    )
    assert (free[2] - free[0], free[3] - free[1]) == (2600, 2600)


def test_move_target_is_anchored_absolute_rect_plus_delta():
    # target = start_rect + (cursor - start_cursor): a pure function of the
    # anchor, so a missed or duplicated WM_MOUSEMOVE cannot compound error
    # and the window can never "stick" away from the cursor.
    assert preview.move_target(
        start_rect=(100, 200, 500, 600), start=(1000, 1000), current=(1030, 985)
    ) == (130, 185, 530, 585)


def test_resize_target_grows_bottom_right_from_anchor():
    # Top-left anchored: start_rect.x/y stay fixed; bottom-right follows the
    # anchored delta, ratio-locked to the source.
    rect = preview.resize_target(
        start_rect=(100, 200, 500, 600),
        start=(1000, 1000),
        current=(1080, 1000),
        aspect=16 / 9,
    )
    assert (rect[0], rect[1]) == (100, 200)
    assert abs((rect[2] - rect[0]) / (rect[3] - rect[1]) - 16 / 9) < 0.02
    assert rect[2] - rect[0] == 480  # 400 + dx of 80


def test_resize_target_clamps_to_min_within_ratio():
    # A huge inward drag floors at the min box (100x80 defaults) while the
    # ratio stays exact: 80 tall -> 142.2 wide.
    rect = preview.resize_target(
        start_rect=(0, 0, 420, 236),
        start=(0, 0),
        current=(-900, -900),
        aspect=16 / 9,
    )
    assert (rect[2] - rect[0], rect[3] - rect[1]) == (143, 80)
    assert abs((rect[2] - rect[0]) / (rect[3] - rect[1]) - 16 / 9) < 0.02


def test_resize_target_unknown_aspect_is_freeform():
    rect = preview.resize_target(
        start_rect=(10, 20, 110, 100),
        start=(0, 0),
        current=(40, 50),
        aspect=None,
    )
    assert rect == (10, 20, 150, 150)


