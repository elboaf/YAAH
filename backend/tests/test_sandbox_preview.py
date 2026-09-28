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


def test_pin_offset_anchors_right_edge():
    yaah = (0, 0, 1000, 800)
    prev = (1024, 100, 1444, 336)
    assert preview.pin_offset(yaah, prev) == (24, 100)


def test_pinned_position_reproduces_anchor_after_yaah_moves():
    yaah = (0, 0, 1000, 800)
    prev = (1024, 100, 1444, 336)
    offset = preview.pin_offset(yaah, prev)
    assert preview.pinned_position((200, 300, 1200, 1100), offset) == (1224, 400)


def test_preview_pinned_config_roundtrip(tmp_path, monkeypatch):
    import backend.agent.config as config

    cfg = tmp_path / "config.json"
    # CONFIG_PATH is bound at import (conftest redirects it to a shared
    # temp file); point it at an isolated file for this test.
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    assert preview.get_preview_pinned() is False
    preview.set_preview_pinned(True)
    assert preview.get_preview_pinned() is True
    # Other sandbox keys survive the write.
    import json

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["sandbox"]["enabled"] is True
    assert data["sandbox"]["preview_pinned"] is True


def test_find_yaah_window_uses_exact_title(monkeypatch):
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


# ---- #122: eve-o-preview-style gestures ----


def test_gesture_action_maps_left_to_move_and_right_to_resize():
    assert preview.gesture_action(True, False) == "move"
    assert preview.gesture_action(False, True) == "resize"


def test_gesture_action_clicks_and_chords_are_inert():
    # A plain click (no drag) must have no other effect: no activation, no
    # context menu (#122). Button chords define no gesture either.
    assert preview.gesture_action(False, False) is None
    assert preview.gesture_action(True, True) is None


def test_resize_keep_ratio_preserves_source_ratio_both_axes():
    # 16:9 source; x-dominant drag drives width, height follows exactly.
    w, h = preview.resize_keep_ratio(1920, 1080, 210, 8, 420, 236)
    assert abs(w / h - 16 / 9) < 0.01
    # y-dominant drag drives height, width follows exactly.
    w2, h2 = preview.resize_keep_ratio(1920, 1080, 8, 94, 420, 236)
    assert abs(w2 / h2 - 16 / 9) < 0.01


def test_resize_keep_ratio_is_top_left_anchored_by_construction():
    # Only sizes are returned; the caller keeps x/y fixed (bottom-right
    # corner follows the drag). A zero drag returns the base size.
    assert preview.resize_keep_ratio(1920, 1080, 0, 0, 420, 236) == (420, 236)


def test_resize_keep_ratio_clamps_to_max_and_stays_in_ratio():
    w, h = preview.resize_keep_ratio(1920, 1080, 5000, 5000, 420, 236)
    assert w <= 640 and h <= 400
    # 16:9 hits the height clamp first: 400 tall -> 711 wide -> clamped to
    # 640 wide -> 360 tall (ratio exact inside the clamp box).
    assert (w, h) == (640, 360)
    assert abs(w / h - 16 / 9) < 0.01


def test_resize_keep_ratio_clamps_to_min_and_stays_in_ratio():
    w, h = preview.resize_keep_ratio(1920, 1080, -5000, -5000, 420, 236)
    assert w >= 100 and h >= 80
    # 16:9 hits the height floor first: 80 tall -> 142.2 wide.
    assert (w, h) == (143, 80)
    assert abs(w / h - 16 / 9) < 0.02


def test_resize_keep_ratio_unknown_source_falls_back_to_base():
    assert preview.resize_keep_ratio(0, 0, 50, 50, 420, 236) == (420, 236)


def test_preview_size_clamp_defaults_match_issue_122():
    assert (preview._PREVIEW_MIN_WIDTH, preview._PREVIEW_MIN_HEIGHT) == (100, 80)
    assert (preview._PREVIEW_MAX_WIDTH, preview._PREVIEW_MAX_HEIGHT) == (640, 400)
