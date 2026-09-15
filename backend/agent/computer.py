"""Computer use (Windows-only): screenshot, window list, mouse/keyboard.

App-testing focus: the tools are real global input (the OS has no scoped
clicks), the system prompt scopes them to the app under test. There is no
consent gate — the panic hotkey plus the user-activity pause carry the
trust load.

Seams, so tests can fake everything:
- ``_capture_screen`` wraps mss (screenshots).
- ``_mouse`` / ``_keyboard`` wrap pynput controllers (input).
- ``_enum_windows`` wraps ctypes EnumWindows (window map).
- ``_activity_idle`` reads the real-input detector.
Everything imports its library lazily inside the function, so importing
this module (as tools.py does on Windows) never loads pynput/mss.
"""
import ctypes
import ctypes.wintypes
import logging
import sys
import threading
import time

log = logging.getLogger(__name__)

WINDOWS = hasattr(ctypes, "windll")

# ---------------------------------------------------------------- schemas

_OBSERVE = {
    "observe": {
        "type": "boolean",
        "description": (
            "Take a screenshot after acting and return it in the same "
            "result (saves a round-trip when chaining). Default off."
        ),
    }
}

COMPUTER_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_ui_tree",
            "description": (
                "Read a window's UI Automation tree: every visible element "
                "with type, name, value, rect and center coordinates. The "
                "PREFERRED way to locate controls and verify UI state — "
                "far more reliable than screenshotting and guessing pixel "
                "positions. Click an element's center with mouse_click. "
                "Falls back to screenshot for pixel-only surfaces (games, "
                "remote streams) that expose no tree."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hwnd": {
                        "type": "integer",
                        "description": "Window handle from list_windows",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Max tree depth (default 6)",
                    },
                    "max_nodes": {
                        "type": "integer",
                        "description": "Max elements returned (default 150)",
                    },
                },
                "required": ["hwnd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": (
                "Capture a screen and attach it so a vision-capable model "
                "can see it. With hwnd, captures the monitor that window "
                "lives on (use this to look at a specific window — "
                "multi-monitor safe). Without hwnd, captures monitor 1 "
                "(the primary) unless monitor says otherwise. Only "
                "screenshot when the task requires seeing the screen — "
                "never to inspect the user's other work. Screenshots go "
                "to the configured model provider."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "monitor": {
                        "type": "integer",
                        "description": "1-based monitor number (default 1 = primary)",
                    },
                    "hwnd": {
                        "type": "integer",
                        "description": (
                            "Window handle from list_windows: capture the "
                            "monitor containing that window (overrides monitor)"
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_windows",
            "description": (
                "List all visible top-level windows with hwnd, title, pid, "
                "process name and rect. The map for finding an app after "
                "launching it via the shell tools."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus_window",
            "description": "Bring a window (by hwnd from list_windows) to the foreground.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hwnd": {"type": "integer", "description": "Window handle from list_windows"},
                    **_OBSERVE,
                },
                "required": ["hwnd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_move",
            "description": "Move the mouse to absolute desktop coordinates.",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, **_OBSERVE},
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_click",
            "description": "Click at absolute desktop coordinates. The workhorse.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right"], "description": "Default left"},
                    "double": {"type": "boolean", "description": "Double-click (default false)"},
                    **_OBSERVE,
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_scroll",
            "description": "Scroll at a position (positive amount = up).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "amount": {"type": "integer"},
                    **_OBSERVE,
                },
                "required": ["x", "y", "amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type unicode text into the focused window (focus first).",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}, **_OBSERVE},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": 'Press a key or chord: "enter", "esc", "ctrl+s", "alt+f4".',
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}, **_OBSERVE},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Sleep for UI paint / load. Cheaper than a retry storm.",
            "parameters": {
                "type": "object",
                "properties": {"seconds": {"type": "number", "description": "Default 1.0, max 30"}},
            },
        },
    },
]

COMPUTER_EXECUTORS: dict = {}  # filled below


# ---------------------------------------------------------------- screenshots

def _capture_screen(monitor: int = 1) -> tuple[bytes, int, int]:
    """PNG bytes + size of one monitor (1-based). Imports mss lazily."""
    import mss
    import mss.tools

    with mss.mss() as sct:
        # sct.monitors[0] is the virtual-all bounding box; 1.. are real monitors.
        idx = max(1, int(monitor))
        if idx >= len(sct.monitors):
            raise ValueError(
                f"monitor {monitor} not found; {len(sct.monitors) - 1} monitor(s) present"
            )
        shot = sct.grab(sct.monitors[idx])
        png = mss.tools.to_png(shot.rgb, shot.size)
        return png, shot.width, shot.height


def _monitors() -> list[dict]:
    """Real monitors in EnumDisplayMonitors order — the same order mss
    indexes monitors[1..] with, so position+1 is the mss monitor number."""
    user32 = ctypes.windll.user32
    out: list[dict] = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.wintypes.RECT), ctypes.c_void_p,
    )

    def _on_monitor(hmon, _hdc, lprect, _lparam):
        rect = ctypes.cast(lprect, ctypes.POINTER(ctypes.wintypes.RECT)).contents
        out.append(
            {
                "handle": int(hmon) if hmon else 0,
                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                "monitor": len(out) + 1,
            }
        )
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_on_monitor), None)
    return out


def _monitor_for_rect(rect: list[int]) -> int:
    """1-based monitor number containing the center of rect (nearest if
    none contains it, e.g. a minimized window at -32000)."""
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    mons = _monitors()
    if not mons:
        return 1
    for m in mons:
        r = m["rect"]
        if r[0] <= cx < r[2] and r[1] <= cy < r[3]:
            return m["monitor"]
    # Nearest by squared distance to the monitor rect's center.
    def _dist(m):
        r = m["rect"]
        mx, my = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
        return (mx - cx) ** 2 + (my - cy) ** 2

    return min(mons, key=_dist)["monitor"]


def _monitor_for_window(hwnd: int) -> int:
    user32 = ctypes.windll.user32
    rect = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 1
    return _monitor_for_rect([rect.left, rect.top, rect.right, rect.bottom])


def _screenshot_result(monitor: int = 1) -> dict:
    from backend.agent.imagedata import save_bytes

    png, w, h = _capture_screen(monitor)
    rel = save_bytes(png, "png", "screenshots")
    return {"image": rel, "monitor": monitor, "size": [w, h]}


async def screenshot(workspace: str = "", monitor: int = 1, hwnd: int = 0) -> dict:
    try:
        if hwnd:
            mon = _monitor_for_window(int(hwnd))
            result = _screenshot_result(mon)
            result["hwnd"] = int(hwnd)
            return result
        return _screenshot_result(monitor)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- windows

def _enum_windows() -> list[dict]:
    """Visible top-level windows via ctypes (user32). Imports lazily so the
    module stays importable in tests with fake window lists."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    results: list[dict] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def _process_name(pid: int) -> str:
        k32 = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not k32:
            return "?"
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint(len(buf))
            if kernel32.QueryFullProcessImageNameW(k32, 0, buf, ctypes.byref(size)):
                return buf.value.replace("\\", "/").rsplit("/", 1)[-1]
            return "?"
        finally:
            kernel32.CloseHandle(k32)

    def _on_window(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1) if length else None
        if title:
            user32.GetWindowTextW(hwnd, title, length + 1)
        pid = ctypes.c_uint()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        results.append(
            {
                "hwnd": int(hwnd),
                "title": title.value if title else "",
                "pid": pid.value,
                "process": _process_name(pid.value),
                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                "monitor": _monitor_for_rect(
                    [rect.left, rect.top, rect.right, rect.bottom]
                ),
            }
        )
        return True

    user32.EnumWindows(WNDENUMPROC(_on_window), None)
    return results


async def list_windows(workspace: str = "") -> dict:
    try:
        wins = _enum_windows()
        return {"windows": wins, "count": len(wins)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


async def focus_window(workspace: str = "", hwnd: int = 0, observe: bool = False) -> dict:
    paused = _pause_check()
    if paused:
        return paused
    try:
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return {"error": f"hwnd {hwnd} is not a valid window"}
        user32.SetForegroundWindow(hwnd)
        result: dict = {"focused": int(hwnd), "ok": True}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    if observe:
        result.update(_screenshot_result())
    return result


# ---------------------------------------------------------------- UI Automation tree

def _read_uia_tree(hwnd: int, max_depth: int, max_nodes: int) -> dict:
    """Walk a window's UIA tree (structured UI: no vision needed). Imports
    uiautomation lazily. Returns a flat element list; each carries its
    tree path and center coordinates so mouse_click can act on it."""
    import uiautomation as uia

    root = uia.ControlFromHandle(int(hwnd))
    if root is None:
        raise ValueError(f"hwnd {hwnd} exposes no UI Automation tree")

    elements: list[dict] = []
    truncated = False

    def _short(v, limit: int = 80) -> str:
        v = " ".join(str(v).split())
        return v if len(v) <= limit else v[: limit - 1] + "…"

    def _walk(ctrl, path: str, depth: int):
        nonlocal truncated
        if len(elements) >= max_nodes:
            truncated = True
            return
        try:
            rect = ctrl.BoundingRectangle
            has_rect = rect.width() > 0 and rect.height() > 0
        except Exception:  # noqa: BLE001 — COM property can throw per element
            rect, has_rect = None, False
        if has_rect:
            entry: dict = {
                "path": path,
                "type": ctrl.ControlTypeName,
                "name": _short(ctrl.Name) if ctrl.Name else "",
            }
            if ctrl.AutomationId:
                entry["id"] = _short(ctrl.AutomationId, 40)
            if not ctrl.IsEnabled:
                entry["disabled"] = True
            entry["center"] = [
                int(rect.xcenter()), int(rect.ycenter()),
            ]
            try:
                if ctrl.IsValuePatternAvailable:
                    val = ctrl.GetValuePattern().Value
                    if val:
                        entry["value"] = _short(val, 200)
            except Exception:  # noqa: BLE001
                pass
            elements.append(entry)
        if depth >= max_depth:
            truncated = truncated or bool(ctrl.GetChildren())
            return
        for i, child in enumerate(ctrl.GetChildren()):
            _walk(child, f"{path}.{i}", depth + 1)
            if len(elements) >= max_nodes:
                truncated = True
                return

    _walk(root, "0", 0)
    return {
        "elements": elements,
        "count": len(elements),
        "truncated": truncated,
        "process": _short(getattr(root, "ClassName", "") or "", 40),
    }


async def read_ui_tree(
    workspace: str = "", hwnd: int = 0, max_depth: int = 6, max_nodes: int = 150
) -> dict:
    try:
        result = _read_uia_tree(int(hwnd), max(1, min(int(max_depth), 12)),
                                max(1, min(int(max_nodes), 400)))
        result["hwnd"] = int(hwnd)
        return result
    except Exception as e:  # noqa: BLE001
        return {
            "error": (
                f"{type(e).__name__}: {e} — this window may be pixel-only; "
                "use screenshot(hwnd=...) instead"
            )
        }


# ---------------------------------------------------------------- activity detector

# The low-level hooks must not count the agent's own SendInput-injected
# events, or every automation action pauses the next one forever. LLMHF_
# INJECTED / LLKHF_INJECTED are deterministic: hardware events never carry
# them. Keep the timing-window approach only as the fallback if the hook
# proves fragile (it is race-prone; do not reach for it first).
# Injected-event flags differ per hook: LLMHF_INJECTED = 0x1 (+0x2 for
# lower-integrity) on WH_MOUSE_LL, LLKHF_INJECTED = 0x10 on WH_KEYBOARD_LL.
# (Verified live: a SendInput move reports flags 0x1, hardware events 0x0.)
_LLMHF_INJECTED = 0x1 | 0x2
_LLKHF_INJECTED = 0x10
WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
_PM_REMOVE = 1


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", ctypes.wintypes.POINT),
        ("mouseData", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _Activity:
    """Tracks the monotonic time of the last REAL (non-injected) user input
    from WH_MOUSE_LL / WH_KEYBOARD_LL hooks on a daemon thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last = 0.0
        self._started = False

    def _note(self):
        with self._lock:
            self._last = time.monotonic()

    def idle_seconds(self) -> float | None:
        """Seconds since the last real user input; None while not started
        (which disables the pause — tests and non-Windows pass through)."""
        with self._lock:
            if self._last == 0.0:
                return None
            return time.monotonic() - self._last

    def start(self):
        if self._started or not WINDOWS:
            return
        self._started = True
        for hook_id, struct_ty, injected_mask in (
            (WH_MOUSE_LL, _MSLLHOOKSTRUCT, _LLMHF_INJECTED),
            (WH_KEYBOARD_LL, _KBDLLHOOKSTRUCT, _LLKHF_INJECTED),
        ):
            threading.Thread(
                target=self._hook_thread,
                args=(hook_id, struct_ty, injected_mask),
                daemon=True,
                name=f"yaah-activity-{hook_id}",
            ).start()

    def _hook_thread(self, hook_id, struct_ty, injected_mask):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        def _callback(ncode, wparam, lparam):
            if ncode >= 0:
                flags = ctypes.cast(lparam, ctypes.POINTER(struct_ty)).contents.flags
                if not flags & injected_mask:
                    self._note()
            return user32.CallNextHookEx(None, ncode, wparam, lparam)

        # Declare the ABI once (LRESULT = ssize_t on 64-bit): without
        # argtypes, each thread's separately-built WINFUNCTYPE counts as a
        # different type and SetWindowsHookExW rejects the trampoline.
        hookproc_ty = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, ctypes.c_ssize_t, ctypes.c_ssize_t
        )
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, hookproc_ty, ctypes.c_void_p, ctypes.c_uint]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t, ctypes.c_ssize_t]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t

        # Keep the trampoline alive for the thread's lifetime.
        proc = hookproc_ty(_callback)
        hook = user32.SetWindowsHookExW(hook_id, proc, None, 0)
        if not hook:
            log.warning("activity hook %s failed to install", hook_id)
            return
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(hook)


_activity = _Activity()


def _activity_idle() -> float | None:
    return _activity.idle_seconds()


# ---------------------------------------------------------------- input tools

PAUSE_SECONDS = 2.0


def _pause_check() -> dict | None:
    """The user-activity pause contract: injected actions refuse while real
    input is recent, so the user always gets the mouse back mid-action."""
    idle = _activity_idle()
    if idle is not None and idle < PAUSE_SECONDS:
        return {
            "error": (
                f"user-activity pause: real input {idle:.1f}s ago — screenshot "
                "to re-verify, then retry when the user is idle"
            ),
            "paused": True,
        }
    return None


class _FakeControllers:
    """Set by tests (computer._mouse = lambda: fake). Real impls lazy-import."""

    def __init__(self):
        self.position = (0, 0)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _U)]


_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN = 76, 77
_SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 78, 79
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000


def _send_move_abs(x: int, y: int):
    """Absolute mouse move via SendInput. MUST be SendInput, not pynput's
    position setter: SetCursorPos reaches the low-level hook WITHOUT the
    injected flag, so the activity detector would read our own moves as
    real user input and pause every subsequent action forever. SendInput
    carries LLMHF_INJECTED and the filter ignores it."""
    user32 = ctypes.windll.user32
    vx, vy = user32.GetSystemMetrics(_SM_XVIRTUALSCREEN), user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
    vw, vh = user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
    ax = (int(x) - vx) * 65535 // max(1, vw - 1)
    ay = (int(y) - vy) * 65535 // max(1, vh - 1)
    inp = _INPUT(
        type=0,
        mi=_MOUSEINPUT(
            dx=ax, dy=ay, mouseData=0,
            dwFlags=_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
            time=0, dwExtraInfo=None,
        ),
    )
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _mouse():
    from pynput import mouse

    return mouse.Controller()


def _keyboard():
    from pynput import keyboard

    return keyboard.Controller()


def _finish(result: dict, observe: bool) -> dict:
    if observe and "error" not in result:
        try:
            result.update(_screenshot_result())
        except Exception as e:  # noqa: BLE001
            result["observe_error"] = f"{type(e).__name__}: {e}"
    return result


async def mouse_move(workspace: str = "", x: int = 0, y: int = 0, observe: bool = False) -> dict:
    paused = _pause_check()
    if paused:
        return paused
    try:
        _send_move_abs(int(x), int(y))
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return _finish({"moved": [int(x), int(y)], "ok": True}, observe)


async def mouse_click(
    workspace: str = "",
    x: int = 0,
    y: int = 0,
    button: str = "left",
    double: bool = False,
    observe: bool = False,
) -> dict:
    paused = _pause_check()
    if paused:
        return paused
    if button not in ("left", "right"):
        return {"error": f"bad button: {button}"}
    try:
        from pynput import mouse as pmouse

        _send_move_abs(int(x), int(y))
        _mouse().click(pmouse.Button[button], 2 if double else 1)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return _finish(
        {"clicked": [int(x), int(y)], "button": button, "double": double, "ok": True},
        observe,
    )


async def mouse_scroll(
    workspace: str = "", x: int = 0, y: int = 0, amount: int = 3, observe: bool = False
) -> dict:
    paused = _pause_check()
    if paused:
        return paused
    try:
        _send_move_abs(int(x), int(y))
        _mouse().scroll(0, int(amount))
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return _finish({"scrolled": int(amount), "at": [int(x), int(y)], "ok": True}, observe)


async def type_text(workspace: str = "", text: str = "", observe: bool = False) -> dict:
    paused = _pause_check()
    if paused:
        return paused
    try:
        _keyboard().type(text)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return _finish({"typed": len(text), "ok": True}, observe)


async def press_key(workspace: str = "", key: str = "", observe: bool = False) -> dict:
    paused = _pause_check()
    if paused:
        return paused
    parts = [p.strip() for p in (key or "").split("+") if p.strip()]
    if not parts:
        return {"error": "bad key: empty"}
    try:
        from pynput import keyboard as pkb

        mods = [_parse_key(p, pkb) for p in parts[:-1]]
        last = _parse_key(parts[-1], pkb)
        kb = _keyboard()
        with kb.pressed(*mods):
            kb.tap(last)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return _finish({"pressed": key, "ok": True}, observe)


def _parse_key(name: str, pkb):
    low = name.lower()
    alias = {"esc": "esc", "win": "cmd", "super": "cmd", "return": "enter"}
    low = alias.get(low, low)
    try:
        return pkb.Key[low]
    except KeyError:
        return pkb.KeyCode.from_char(low)


async def wait(workspace: str = "", seconds: float = 1.0) -> dict:
    import asyncio

    seconds = min(max(0.0, float(seconds)), 30.0)
    await asyncio.sleep(seconds)
    return {"waited": seconds, "ok": True}


COMPUTER_EXECUTORS.update(
    {
        "screenshot": screenshot,
        "list_windows": list_windows,
        "focus_window": focus_window,
        "read_ui_tree": read_ui_tree,
        "mouse_move": mouse_move,
        "mouse_click": mouse_click,
        "mouse_scroll": mouse_scroll,
        "type_text": type_text,
        "press_key": press_key,
        "wait": wait,
    }
)


# ---------------------------------------------------------------- panic hotkey

def panic_hotkey_combo() -> str:
    """The configured pynput hotkey combo, validated by construction; falls
    back to the default on anything unusable (bad/empty config)."""
    default = "<ctrl>+<alt>+y"  # pynput format: single chars take no <>
    from backend.agent.config import load_config

    combo = str(
        (load_config().get("computer_use") or {}).get("panic_hotkey") or default
    ).strip()
    if not combo:
        return default
    try:
        from pynput import keyboard

        # GlobalHotKeys maps combo -> callback; constructing it validates.
        keyboard.GlobalHotKeys({combo: lambda: None})
        return combo
    except Exception:  # noqa: BLE001
        log.warning("bad computer_use.panic_hotkey %r; using default", combo)
        return default


def panic_notice() -> str:
    """One-line prompt sentence about the panic hotkey (best effort)."""
    try:
        combo = panic_hotkey_combo()
    except Exception:  # noqa: BLE001
        combo = "<ctrl>+<alt>+<y>"
    return (
        f"Urgent stop: pressing {combo} (the panic hotkey) anywhere on the "
        "desktop cancels every running turn immediately."
    )


def cancel_all_turns():
    """Panic: cancel every running agent turn. Local import avoids the
    loop -> tools -> computer -> loop cycle."""
    from backend.agent import loop as loop_mod

    for conv_id in list(loop_mod._cancel_events):
        loop_mod.cancel_agent(conv_id)


def start_panic_hotkey():
    combo = panic_hotkey_combo()
    from pynput import keyboard

    keyboard.GlobalHotKeys({combo: cancel_all_turns}).start()
    log.info("panic hotkey active: %s", combo)


def start_background():
    """Backend startup hook (Windows only, never fatal): the real-input
    detector plus the panic hotkey listener. Skipped under pytest — tests
    must stay hermetic (no real hooks, no real hotkey)."""
    if not WINDOWS or "pytest" in sys.modules:
        return
    try:
        _activity.start()
    except Exception as e:  # noqa: BLE001
        log.warning("activity detector failed to start: %s", e)
    try:
        start_panic_hotkey()
    except Exception as e:  # noqa: BLE001
        log.warning("panic hotkey failed to start: %s", e)
