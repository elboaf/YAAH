"""Computer-use tool layer (backend/agent/computer.py), tested against
fakes — no real input injection or capture ever runs here. One live
Notepad drive is marked live and gated on YAAH_LIVE_COMPUTER=1; never
run in CI (an unattended desktop would eat stray clicks)."""
import asyncio
import time

import pytest

from backend.agent import computer as computer_mod


@pytest.fixture
def fake_activity(monkeypatch):
    """Idle forever: the pause never fires unless a test asks for it."""
    state = {"idle": None}

    def _idle():
        return state["idle"]

    monkeypatch.setattr(computer_mod, "_activity_idle", _idle)
    return state


@pytest.fixture
def fake_input(monkeypatch):
    class FakeMouse:
        def __init__(self):
            self.position = (0, 0)
            self.calls = []

        def click(self, button, count):
            self.calls.append(("click", button, count))

        def scroll(self, dx, dy):
            self.calls.append(("scroll", dx, dy))

    class FakeKeyboard:
        def __init__(self):
            self.typed = []
            self.taps = []

            class _Ctx:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            self._ctx = _Ctx

        def type(self, text):
            self.typed.append(text)

        def tap(self, key):
            self.taps.append(key)

        def pressed(self, *mods):
            self.taps.extend(mods)
            return self._ctx()

    mouse, keyboard = FakeMouse(), FakeKeyboard()
    monkeypatch.setattr(computer_mod, "_mouse", lambda: mouse)
    monkeypatch.setattr(computer_mod, "_keyboard", lambda: keyboard)
    moves = []

    def _fake_move_abs(x, y):
        moves.append((x, y))

    monkeypatch.setattr(computer_mod, "_send_move_abs", _fake_move_abs)
    return mouse, keyboard, moves


@pytest.fixture
def fake_capture(monkeypatch):
    calls = []

    def _shot(monitor=1):
        calls.append(monitor)
        return {"image": f"screenshots/fake{monitor}.png", "monitor": monitor, "size": [800, 600]}

    monkeypatch.setattr(computer_mod, "_screenshot_result", _shot)
    return calls


# ---------------------------------------------------------------- pause contract

def test_pause_refuses_recent_real_input(fake_activity, monkeypatch):
    fake_activity["idle"] = 0.5
    res = asyncio.run(computer_mod.mouse_move(x=1, y=2))
    assert res["paused"] is True
    assert "user-activity pause" in res["error"]


def test_pause_allows_idle_or_unstarted(fake_activity, fake_input):
    # idle=None (detector never fired) and idle>=2.0 both act.
    asyncio.run(computer_mod.mouse_move(x=1, y=2))
    fake_activity["idle"] = 2.5
    res = asyncio.run(computer_mod.mouse_click(x=3, y=4))
    assert res["ok"] is True and res["clicked"] == [3, 4]


@pytest.mark.parametrize("tool,args", [
    ("focus_window", {"hwnd": 1}),
    ("mouse_move", {"x": 1, "y": 1}),
    ("mouse_click", {"x": 1, "y": 1}),
    ("mouse_scroll", {"x": 1, "y": 1, "amount": 1}),
    ("type_text", {"text": "x"}),
    ("press_key", {"key": "enter"}),
])
def test_pause_covers_every_input_tool(fake_activity, fake_input, tool, args):
    fake_activity["idle"] = 0.1
    fn = getattr(computer_mod, tool)
    res = asyncio.run(fn(**args))
    assert res.get("paused") is True


def test_pause_never_applies_to_screenshot_or_wait(fake_activity, fake_capture):
    fake_activity["idle"] = 0.1
    res = asyncio.run(computer_mod.screenshot())
    assert "image" in res
    res = asyncio.run(computer_mod.wait(seconds=0.01))
    assert res["ok"] is True


# ---------------------------------------------------------------- observe param

def test_observe_attaches_post_action_screenshot(fake_activity, fake_input, fake_capture):
    res = asyncio.run(computer_mod.mouse_click(x=5, y=6, observe=True))
    assert res["image"] == "screenshots/fake1.png"
    assert fake_capture == [1]


def test_observe_default_off(fake_activity, fake_input, fake_capture):
    res = asyncio.run(computer_mod.type_text(text="hi"))
    assert "image" not in res
    assert fake_capture == []


def test_observe_failure_does_not_mask_action(fake_activity, fake_input, monkeypatch):
    def _boom(monitor=1):
        raise RuntimeError("no monitor")

    monkeypatch.setattr(computer_mod, "_screenshot_result", _boom)
    res = asyncio.run(computer_mod.press_key(key="ctrl+s", observe=True))
    assert res["ok"] is True
    assert "RuntimeError" in res["observe_error"]


# ---------------------------------------------------------------- input tools

def test_click_button_and_double(fake_input):
    mouse, _, moves = fake_input
    asyncio.run(computer_mod.mouse_click(x=1, y=2))
    asyncio.run(computer_mod.mouse_click(x=1, y=2, button="right", double=True))
    # pynput hands the controller a Button enum; check by name.
    assert mouse.calls[0][0] == "click" and mouse.calls[0][1].name == "left"
    assert mouse.calls[0][2] == 1
    assert mouse.calls[1][1].name == "right" and mouse.calls[1][2] == 2
    # Positioning goes through SendInput (injected -> ignored by the
    # activity filter), never pynput's SetCursorPos position setter.
    assert moves == [(1, 2), (1, 2)]


def test_click_rejects_bad_button(fake_input):
    res = asyncio.run(computer_mod.mouse_click(x=1, y=2, button="middle"))
    assert "error" in res


def test_press_key_chord_and_named_key(fake_input):
    _, kb, _ = fake_input
    asyncio.run(computer_mod.press_key(key="ctrl+s"))
    asyncio.run(computer_mod.press_key(key="enter"))
    asyncio.run(computer_mod.press_key(key="esc"))
    # ctrl down, s tap, ctrl up-ish (order preserved), named keys resolve.
    assert len(kb.taps) == 4
    res = asyncio.run(computer_mod.press_key(key=""))
    assert "error" in res


def test_type_text(fake_input):
    _, kb, _ = fake_input
    res = asyncio.run(computer_mod.type_text(text="héllo"))
    assert res["typed"] == 5 and kb.typed == ["héllo"]


def test_wait_caps_at_30s(monkeypatch):
    slept = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        slept.append(s)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    res = asyncio.run(computer_mod.wait(seconds=999))
    assert res["waited"] == 30.0
    assert slept == [30.0]


# ---------------------------------------------------------------- screenshots + windows

def test_screenshot_returns_image_result(fake_capture):
    res = asyncio.run(computer_mod.screenshot(monitor=2))
    assert res["image"] == "screenshots/fake2.png"
    assert res["size"] == [800, 600]


def test_list_windows_shape(monkeypatch):
    captured = {}
    fake_wins = [{"hwnd": 42, "title": "Notepad", "pid": 7,
                  "process": "notepad.exe", "rect": [0, 0, 100, 100],
                  "monitor": 2}]

    def fake_enum():
        # Simulate _enum_windows' per-window monitor tagging.
        captured["called"] = True
        return fake_wins

    monkeypatch.setattr(computer_mod, "_enum_windows", fake_enum)
    res = asyncio.run(computer_mod.list_windows())
    assert res["count"] == 1
    assert res["windows"][0]["process"] == "notepad.exe"
    assert res["windows"][0]["monitor"] == 2


def test_monitor_for_rect_containment_and_nearest():
    if not computer_mod.WINDOWS:
        pytest.skip("windows-only")
    mons = computer_mod._monitors()
    if len(mons) < 2:
        pytest.skip("needs multiple monitors")
    r0, r1 = mons[0]["rect"], mons[1]["rect"]
    # Center of monitor 1's rect maps to monitor 1, etc.
    assert computer_mod._monitor_for_rect(r0) == 1
    assert computer_mod._monitor_for_rect(r1) == 2
    # A minimized window sits at -32000: must fall back to nearest, not crash.
    assert computer_mod._monitor_for_rect([-32000, -32000, -31800, -31900]) in (1, 2)


def test_screenshot_hwnd_captures_window_monitor(fake_capture, monkeypatch):
    monkeypatch.setattr(computer_mod, "_monitor_for_window", lambda hwnd: 2)
    res = asyncio.run(computer_mod.screenshot(hwnd=1234))
    assert res["monitor"] == 2
    assert res["hwnd"] == 1234
    assert fake_capture == [2]  # captured monitor 2, not the primary


def test_screenshot_monitor_param_still_works(fake_capture):
    res = asyncio.run(computer_mod.screenshot(monitor=1))
    assert res["monitor"] == 1
    assert fake_capture == [1]


def test_focus_window_pause_contract(fake_activity, fake_input):
    if not computer_mod.WINDOWS:
        pytest.skip("windows-only")
    # Can't touch real user32 here; the pause contract is already covered.
    fake_activity["idle"] = 0.1
    res = asyncio.run(computer_mod.focus_window(hwnd=1))
    assert res.get("paused") is True


# ---------------------------------------------------------------- registration + prompt

def test_tools_registered_on_windows():
    from backend.agent.tools import get_schemas

    names = {s["function"]["name"] for s in get_schemas()}
    expected = {"screenshot", "list_windows", "focus_window", "mouse_move",
                "mouse_click", "mouse_scroll", "type_text", "press_key", "wait"}
    if computer_mod.WINDOWS:
        assert expected <= names
    else:
        assert not (expected & names)


def test_execute_tool_dispatches_computer_tools(fake_activity, fake_input):
    from backend.agent.tools import execute_tool

    res = asyncio.run(execute_tool("wait", {"seconds": 0.01}, "ws"))
    assert res["ok"] is True


def test_computer_use_prompt_section_mentions_contract():
    text = computer_mod.panic_notice()
    assert "panic" in text.lower()
    from backend.agent.loop import _computer_use_prompt

    section = _computer_use_prompt()
    assert "user-activity pause" in section
    assert "screenshot" in section


# ---------------------------------------------------------------- activity detector internals

def test_activity_idle_none_before_start():
    act = computer_mod._Activity()
    assert act.idle_seconds() is None


def test_activity_injected_filter_flags():
    # Verified live: SendInput-injected mouse events report flags 0x1
    # (LLMHF_INJECTED); keyboard uses 0x10 (LLKHF_INJECTED); hardware is 0.
    assert computer_mod._LLMHF_INJECTED & 0x1
    assert computer_mod._LLKHF_INJECTED & 0x10
    assert not 0 & computer_mod._LLMHF_INJECTED


def test_activity_note_updates_idle():
    act = computer_mod._Activity()
    act._note()
    idle = act.idle_seconds()
    assert idle is not None and idle < 1.0


def test_start_background_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(computer_mod, "WINDOWS", False)
    computer_mod.start_background()  # must not raise


# ---------------------------------------------------------------- panic hotkey config

def test_panic_hotkey_default_on_missing_config():
    combo = computer_mod.panic_hotkey_combo()
    assert combo == "<ctrl>+<alt>+y"


def test_panic_hotkey_invalid_config_falls_back(monkeypatch):
    import backend.agent.config as config_mod

    monkeypatch.setattr(
        config_mod, "load_config", lambda: {"computer_use": {"panic_hotkey": "bogus"}}
    )
    assert computer_mod.panic_hotkey_combo() == "<ctrl>+<alt>+y"


# ---------------------------------------------------------------- live integration

@pytest.mark.live
def test_live_notepad_drive():
    """YAAH_LIVE_COMPUTER=1 python -m pytest backend/tests/test_computer.py -k live
    Drives real Notepad: launch via shell, type, read back from the file."""
    import os
    import subprocess
    import tempfile

    if not os.environ.get("YAAH_LIVE_COMPUTER"):
        pytest.skip("live computer-use test; set YAAH_LIVE_COMPUTER=1")
    tmp = tempfile.mktemp(suffix=".txt")
    subprocess.Popen(["notepad.exe", tmp])
    try:
        wins = computer_mod._enum_windows()
        target = next(w for w in wins if "notepad" in w["process"].lower())
        asyncio.run(computer_mod.focus_window(hwnd=target["hwnd"]))
        asyncio.run(computer_mod.wait(seconds=1.0))
        asyncio.run(computer_mod.type_text(text="yaah live test"))
        asyncio.run(computer_mod.press_key(key="ctrl+s"))
        asyncio.run(computer_mod.wait(seconds=1.0))
        with open(tmp, encoding="utf-8") as f:
            assert f.read() == "yaah live test"
    finally:
        subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"],
                       capture_output=True)
        if os.path.exists(tmp):
            os.unlink(tmp)
