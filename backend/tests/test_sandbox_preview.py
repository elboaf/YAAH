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
