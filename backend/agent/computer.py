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

# Real host input — the #19/#20 nudge, repeated on every input tool so it
# survives schema-only context windows.
_HOST_INPUT_NOTE = (
    " This is the USER'S REAL mouse/keyboard — never use it to drive an "
    "app running inside the Windows Sandbox; drive the sandbox GUI via "
    "the windows-mcp MCP server (see the sandbox section) instead."
)

_OBSERVE = {
    "observe": {
        "type": "boolean",
        "description": (
            "Take a screenshot after acting and return it in the same "
            "result (saves a round-trip when chaining). For the position "
            "tools this is a small crop around the action point. "
            "Defaults to ON for mouse actions (config "
            "computer_use.observe_default); pass false to skip."
        ),
    }
}

# Mouse x/y are relative to this monitor's origin (what a screenshot of
# that monitor shows); 0 = absolute virtual-desktop coordinates.
_MONITOR_PARAM = {
    "monitor": {
        "type": "integer",
        "description": (
            "Which monitor x/y are relative to: 1-based monitor number "
            "(default 1 = primary), or 0 for absolute virtual-desktop "
            "coordinates. Coordinates you measured in a screenshot of "
            "monitor N are monitor-N-local — pass them with monitor=N "
            "UNCHANGED; do not convert by hand."
        ),
    }
}

# Full documentation for get_help — the schema descriptions stay short;
# what lives here reaches the model only when it asks for a tool's docs.
COMPUTER_HELP_DOCS = {
    "read_ui_tree": (
        "A result with \"truncated\": true is NOT exhaustive — never "
        "conclude an element doesn't exist from a depth- or node-limited "
        "read; raise max_depth/max_nodes or screenshot instead. Falls "
        "back to screenshot for pixel-only surfaces (games, remote "
        "streams) that expose no tree."
    ),
    "screenshot": (
        "Read the coordinate ruler nearest your target and pass that "
        "value to a mouse tool with the same monitor number — never "
        "estimate pixel positions visually. With x/y/w/h the capture is "
        "just that region (desktop coordinates) — use it for precision "
        "targeting and close-ups. Results carry the monitor's "
        "\"origin\": pixel (px,py) in the image is desktop "
        "(origin.x + px, origin.y + py). Without hwnd/region/monitor, "
        "captures monitor 1 (primary). Never screenshot just to inspect "
        "the user's other work."
    ),
    "mouse_drag": (
        "For sliders, drag-and-drop, and text selection. A slow duration "
        "(up to 3s) helps apps that need real drag momentum."
    ),
}

COMPUTER_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_ui_tree",
            "description": (
                "Read a window's UI Automation tree: every visible element "
                "with type, name, value, rect and center coordinates. The "
                "preferred way to locate controls and verify UI state — "
                "far more reliable than screenshotting and guessing pixel "
                "positions. Click an element's center with mouse_click. "
                "A result with \"truncated\": true is NOT exhaustive — "
                "never conclude an element doesn't exist from a "
                "depth- or node-limited read; raise the limits or "
                "screenshot instead."
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
                "Capture a screen (or an x/y/w/h region) and attach it so "
                "a vision-capable model can see it. Captures carry labeled "
                "coordinate rulers — read the ruler, don't estimate. With "
                "hwnd, captures the monitor that window lives on; with "
                "elements=true, draws numbered boxes on its UIA elements "
                "(set-of-marks) and returns id -> name/type/center. With "
                "x/y/w/h the capture is just that region (desktop "
                "coordinates) - use it for precision targeting and "
                "close-ups. Results carry the monitor's \"origin\": pixel "
                "(px,py) in the image is desktop (origin.x + px, "
                "origin.y + py). Only screenshot when the task requires "
                "seeing the screen. Screenshots go to the model provider."
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
                    "elements": {
                        "type": "boolean",
                        "description": (
                            "With hwnd: draw numbered boxes on the window's "
                            "UIA elements (set-of-marks) and return the id -> "
                            "name/type/center list; click an element's center "
                            "with mouse_click. Combines the tree with vision."
                        ),
                    },
                    "x": {"type": "integer", "description": "Region left (desktop coords, with w/h)"},
                    "y": {"type": "integer", "description": "Region top (desktop coords, with w/h)"},
                    "w": {"type": "integer", "description": "Region width"},
                    "h": {"type": "integer", "description": "Region height"},
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
            "description": (
                "Move the mouse; x/y are monitor-local (default primary). "
                "The result reports the REAL cursor position afterwards — "
                "check it before clicking."
                + _HOST_INPUT_NOTE
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    **_MONITOR_PARAM,
                    **_OBSERVE,
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_click",
            "description": (
                "Click at x/y (monitor-local; screenshot coords of monitor "
                "N pass through unchanged with monitor=N). The result "
                "reports the REAL cursor position at click time."
                + _HOST_INPUT_NOTE
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    **_MONITOR_PARAM,
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "Default left",
                    },
                    "double": {"type": "boolean", "description": "Double-click (default false)"},
                    "modifier": {
                        "type": "string",
                        "description": (
                            "Modifier key(s) held during the click, "
                            '+-joined: "shift", "ctrl", "alt", "ctrl+shift"'
                        ),
                    },
                    **_OBSERVE,
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_drag",
            "description": (
                "Press at a start point, drag to an end point, release. "
                "For sliders, drag-and-drop, and text selection. x/y are "
                "monitor-local. A slow duration (up to 3s) helps apps "
                "that need real drag momentum."
                + _HOST_INPUT_NOTE
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_x": {"type": "integer"},
                    "start_y": {"type": "integer"},
                    "x": {"type": "integer", "description": "End x"},
                    "y": {"type": "integer", "description": "End y"},
                    **_MONITOR_PARAM,
                    "button": {
                        "type": "string",
                        "enum": ["left", "right"],
                        "description": "Default left",
                    },
                    "duration": {
                        "type": "number",
                        "description": "Drag time in seconds (default 0.3, max 3)",
                    },
                    **_OBSERVE,
                },
                "required": ["start_x", "start_y", "x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_scroll",
            "description": "Scroll at a position (positive amount = up)." + _HOST_INPUT_NOTE,
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    **_MONITOR_PARAM,
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
            "description": (
                "Type unicode text into the focused window (focus first)."
                + _HOST_INPUT_NOTE
            ),
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
            "description": (
                'Press a key or chord: "enter", "esc", "ctrl+s", "alt+f4".'
                + _HOST_INPUT_NOTE
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "repeat": {
                        "type": "integer",
                        "description": "Times to press (default 1, max 25)",
                    },
                    **_OBSERVE,
                },
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


def _monitor_for_point(x: int, y: int) -> int:
    return _monitor_for_rect([int(x), int(y), int(x), int(y)])


def _monitor_of_foreground() -> int:
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 1
    return _monitor_for_window(hwnd)


def _monitor_rect(monitor: int) -> list[int]:
    mons = _monitors()
    for m in mons:
        if m["monitor"] == monitor:
            return m["rect"]
    raise ValueError(f"monitor {monitor} not found")


def _to_desktop(x: int, y: int, monitor: int) -> tuple[int, int]:
    """Monitor-local x/y -> virtual-desktop coordinates. monitor 0 means
    already-absolute; this is the ONLY place the conversion happens, so
    the model never does offset arithmetic (that's what clicked the wrong
    screen)."""
    if int(monitor) <= 0:
        return int(x), int(y)
    r = _monitor_rect(int(monitor))
    return r[0] + int(x), r[1] + int(y)


def _cursor_pos() -> tuple[int, int]:
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


# observe crops center on the action point; small enough to be cheap,
# big enough to show the control the cursor is on and its neighbours.
OBSERVE_CROP = 400


def _capture_clip(clip: dict) -> tuple[bytes, int, int]:
    import mss
    import mss.tools

    with mss.mss() as sct:
        shot = sct.grab(clip)
        return mss.tools.to_png(shot.rgb, shot.size), shot.width, shot.height


def _clip_around(ax: int, ay: int, size: int = OBSERVE_CROP) -> dict:
    """mss clip centered on a desktop point, clamped to the monitor it's
    on (mss rejects rects that leave a monitor)."""
    m = _monitor_for_point(ax, ay)
    r = _monitor_rect(m)
    half = size // 2
    left = max(r[0], min(ax - half, r[2] - size))
    top = max(r[1], min(ay - half, r[3] - size))
    return {
        "left": left, "top": top,
        "width": min(size, r[2] - r[0]), "height": min(size, r[3] - r[1]),
    }


def _clip_region(x: int, y: int, w: int, h: int) -> dict:
    """mss clip for a desktop-coordinate region, clamped to the monitor
    containing its center."""
    m = _monitor_for_point(x + w // 2, y + h // 2)
    r = _monitor_rect(m)
    left = max(r[0], min(x, r[2] - 1))
    top = max(r[1], min(y, r[3] - 1))
    return {
        "left": left, "top": top,
        "width": max(1, min(w, r[2] - left)), "height": max(1, min(h, r[3] - top)),
    }


def _annotate(png: bytes, w: int, h: int, label_offset: list[int]) -> bytes:
    """Draw labeled coordinate rulers onto a capture so the model can READ
    the monitor-local coordinate near a target instead of estimating it.
    This exists because a 2560px capture is perceived downscaled — visual
    estimation is systematically scaled-off (a dogfood session clicked
    x=500 where 1340 was meant). Labels are monitor-local pixels: the
    exact numbers to pass to mouse_click(monitor=N). Best-effort: any
    failure returns the raw PNG."""
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(png)).convert("RGB")
        _annotate_img(img, w, h, label_offset, 1.0)
        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return png


# Screenshots are downscaled to this long edge before storing: providers
# cap vision inputs around this size anyway, so full-res captures only
# cost tokens and acuity. Anthropic's guidance: 1024x768..1920x1080.
MAX_CAPTURE_EDGE = 1568


def _annotate_img(img, orig_w: int, orig_h: int, label_offset: list[int], scale: float):
    """Draw rulers onto a PIL image. Tick spacing is in monitor-local
    VALUE space (step), positions scale with the image. mutates img."""
    from PIL import ImageDraw

    step = 200 if min(orig_w, orig_h) >= 1000 else 100
    d = ImageDraw.Draw(img)
    img_w, img_h = img.size
    vx = (int(label_offset[0]) // step + 1) * step
    while vx < label_offset[0] + orig_w:
        x = round((vx - label_offset[0]) * scale)
        d.line([(x, 0), (x, img_h)], fill=(110, 110, 110), width=1)
        d.text((x + 3, 3), str(vx), fill=(255, 220, 0))
        d.text((x + 3, img_h - 14), str(vx), fill=(255, 220, 0))
        vx += step
    vy = (int(label_offset[1]) // step + 1) * step
    while vy < label_offset[1] + orig_h:
        y = round((vy - label_offset[1]) * scale)
        d.line([(0, y), (img_w, y)], fill=(110, 110, 110), width=1)
        d.text((3, y + 3), str(vy), fill=(255, 220, 0))
        d.text((img_w - 40, y + 3), str(vy), fill=(255, 220, 0))
        vy += step
    return img


def _store_png(png: bytes, w: int, h: int, monitor: int, origin: list[int], **extra) -> dict:
    from backend.agent.imagedata import save_bytes

    # label_offset maps image pixels to monitor-local coordinates: a full
    # monitor shot starts at local (0,0); a crop's top-left sits at
    # crop_origin - monitor_origin inside the monitor.
    mon_origin = _monitor_rect(monitor)[:2]
    label_offset = [origin[0] - mon_origin[0], origin[1] - mon_origin[1]]
    try:
        import io

        from PIL import Image

        scale = 1.0
        img = Image.open(io.BytesIO(png)).convert("RGB")
        if max(img.size) > MAX_CAPTURE_EDGE:
            scale = MAX_CAPTURE_EDGE / max(img.size)
            img = img.resize(
                (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                Image.LANCZOS,
            )
        _annotate_img(img, w, h, label_offset, scale)
        out = io.BytesIO()
        img.save(out, "PNG")
        png = out.getvalue()
        w, h = img.size
    except Exception:  # noqa: BLE001 — store the raw capture un-annotated
        pass
    rel = save_bytes(png, "png", "screenshots")
    out = {"image": rel, "monitor": monitor, "size": [w, h], "origin": origin}
    out.update(extra)
    return out


def _screenshot_result(monitor: int = 1) -> dict:
    png, w, h = _capture_screen(monitor)
    return _store_png(png, w, h, monitor, _monitor_rect(monitor)[:2])


def _observe_crop_result(ax: int, ay: int) -> dict:
    """Small crop centered on the action point instead of the full
    monitor — the close-up the move→verify→click loop needs, without the
    token cost of a whole screen."""
    m = _monitor_for_point(ax, ay)
    clip = _clip_around(ax, ay)
    png, w, h = _capture_clip(clip)
    return _store_png(
        png, w, h, m, [clip["left"], clip["top"]], crop_center=[ax, ay]
    )


def _som_overlay(
    rel: str, stored_w: int, stored_h: int, monitor: int, hwnd: int, limit: int = 30
) -> dict:
    """Set-of-marks: draw numbered boxes on the stored screenshot for the
    window's UIA elements and return the id -> element list. Centers are
    monitor-local, i.e. directly usable as mouse_click coordinates."""
    from backend.agent.imagedata import IMAGES_ROOT

    tree = _read_uia_tree(int(hwnd), 8, 120)
    mon_origin = _monitor_rect(monitor)[:2]
    mon_w = max(1, _monitor_rect(monitor)[2] - _monitor_rect(monitor)[0])
    sx = stored_w / mon_w

    out: list[dict] = []
    for el in tree["elements"]:
        if not el.get("name") or el.get("disabled"):
            continue
        cx, cy = el["center"]
        lx, ly = round((cx - mon_origin[0]) * sx), round((cy - mon_origin[1]) * sx)
        if not (0 <= lx < stored_w and 0 <= ly < stored_h):
            continue
        out.append(
            {
                "id": len(out),
                "name": el["name"],
                "type": el["type"],
                "center": [cx - mon_origin[0], cy - mon_origin[1]],
            }
        )
        if len(out) >= limit:
            break

    try:
        import io

        from PIL import Image, ImageDraw

        path = IMAGES_ROOT / rel
        img = Image.open(path).convert("RGB")
        d = ImageDraw.Draw(img)
        for el in out:
            lx = round((el["center"][0]) * sx)
            ly = round((el["center"][1]) * sx)
            d.rectangle([lx - 11, ly - 11, lx + 11, ly + 11], outline=(255, 60, 60), width=2)
            d.text((lx + 13, ly - 8), str(el["id"]), fill=(255, 60, 60))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        path.write_bytes(buf.getvalue())
    except Exception as e:  # noqa: BLE001 — boxes are best-effort
        return {"elements": out, "truncated": tree["truncated"],
                "overlay_error": f"{type(e).__name__}: {e}"}
    return {"elements": out, "truncated": tree["truncated"]}


async def screenshot(
    workspace: str = "",
    monitor: int = 1,
    hwnd: int = 0,
    elements: bool = False,
    x: int = 0,
    y: int = 0,
    w: int = 0,
    h: int = 0,
) -> dict:
    try:
        if w > 0 and h > 0:
            clip = _clip_region(int(x), int(y), int(w), int(h))
            png, cw, ch = _capture_clip(clip)
            mon = _monitor_for_point(
                clip["left"] + clip["width"] // 2, clip["top"] + clip["height"] // 2
            )
            return _store_png(png, cw, ch, mon, [clip["left"], clip["top"]],
                              region=[x, y, w, h])
        if hwnd:
            mon = _monitor_for_window(int(hwnd))
            result = _screenshot_result(mon)
            result["hwnd"] = int(hwnd)
            if elements:
                try:
                    result.update(
                        _som_overlay(result["image"], result["size"][0],
                                     result["size"][1], mon, int(hwnd))
                    )
                except Exception as e:  # noqa: BLE001
                    result["elements_error"] = f"{type(e).__name__}: {e}"
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
        # A minimized window can't take focus; restore it first.
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # Windows refuses foreground switches initiated by background
        # processes. A brief ALT press makes the OS treat the switch as
        # user-initiated (the standard workaround); without it this
        # silently fails whenever another app has focus.
        user32.keybd_event(0x12, 0, 0, 0)  # ALT down
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)  # ALT up
        ok = user32.GetForegroundWindow() == hwnd
        result: dict = {"focused": int(hwnd), "ok": bool(ok)}
        if not ok:
            result["note"] = (
                "the OS did not switch foreground; the window may be "
                "fullscreen-exclusive — screenshot(hwnd=...) still works"
            )
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    if observe:
        result.update(_screenshot_result(_monitor_for_window(hwnd)))
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
                f"user-activity pause: real input {idle:.1f}s ago — the user "
                "is at the machine; wait a few seconds and retry when idle"
            ),
            "paused": True,
        }
    return None


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


def _finish(result: dict, observe: bool, monitor: int | None = None) -> dict:
    """Attach the post-action screenshot when observe is set. The capture
    MUST be the monitor the action happened on — the primary-monitor shot
    made a dogfood session conclude correct clicks had 'landed on the
    wrong monitor'. Position-less tools (type/press) use the focused
    window's monitor."""
    if observe and "error" not in result:
        try:
            mon = monitor if monitor is not None else _monitor_of_foreground()
            result.update(_screenshot_result(mon))
        except Exception as e:  # noqa: BLE001
            result["observe_error"] = f"{type(e).__name__}: {e}"
    return result


def _cursor_feedback(result: dict, ax: int, ay: int, observe: bool) -> dict:
    """The move→verify→click checkpoint: every position action reports the
    REAL cursor position (GetCursorPos) and which monitor it landed on, so
    a coordinate mistake is visible before the click does damage. observe
    crops around the point instead of capturing the whole monitor."""
    cx, cy = _cursor_pos()
    result["requested_desktop"] = [ax, ay]
    result["cursor"] = [cx, cy]
    result["cursor_monitor"] = _monitor_for_point(cx, cy)
    result["on_target"] = abs(cx - ax) <= 2 and abs(cy - ay) <= 2
    if observe and "error" not in result:
        try:
            result.update(_observe_crop_result(cx, cy))
        except Exception as e:  # noqa: BLE001
            result["observe_error"] = f"{type(e).__name__}: {e}"
    return result


def _observe_default() -> bool:
    from backend.agent.config import load_config

    return bool((load_config().get("computer_use") or {}).get("observe_default", True))


def _resolve_observe(observe: bool | None) -> bool:
    return _observe_default() if observe is None else bool(observe)


def _parse_modifiers(modifier: str):
    """'+-joined' modifier names -> pynput Key objects ([] on garbage)."""
    if not modifier:
        return []
    try:
        from pynput import keyboard as pkb

        return [
            _parse_key(p, pkb)
            for p in str(modifier).replace("+", "+").split("+")
            if p.strip()
        ]
    except Exception:  # noqa: BLE001
        return []


async def mouse_move(
    workspace: str = "", x: int = 0, y: int = 0, monitor: int = 1,
    observe: bool | None = None,
) -> dict:
    paused = _pause_check()
    if paused:
        return paused
    try:
        ax, ay = _to_desktop(x, y, monitor)
        _send_move_abs(ax, ay)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return _cursor_feedback(
        {"moved": [int(x), int(y)], "monitor": int(monitor), "ok": True},
        ax, ay, _resolve_observe(observe),
    )


async def mouse_click(
    workspace: str = "",
    x: int = 0,
    y: int = 0,
    monitor: int = 1,
    button: str = "left",
    double: bool = False,
    modifier: str = "",
    observe: bool | None = None,
) -> dict:
    paused = _pause_check()
    if paused:
        return paused
    if button not in ("left", "right", "middle"):
        return {"error": f"bad button: {button}"}
    try:
        from pynput import mouse as pmouse

        ax, ay = _to_desktop(x, y, monitor)
        _send_move_abs(ax, ay)
        mods = _parse_modifiers(modifier)
        m = _mouse()
        if mods:
            with _keyboard().pressed(*mods):
                m.click(pmouse.Button[button], 2 if double else 1)
        else:
            m.click(pmouse.Button[button], 2 if double else 1)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return _cursor_feedback(
        {
            "clicked": [int(x), int(y)], "monitor": int(monitor),
            "button": button, "double": double, "ok": True,
        },
        ax, ay, _resolve_observe(observe),
    )


async def mouse_drag(
    workspace: str = "",
    start_x: int = 0,
    start_y: int = 0,
    x: int = 0,
    y: int = 0,
    monitor: int = 1,
    button: str = "left",
    duration: float = 0.3,
    observe: bool | None = None,
) -> dict:
    import asyncio

    paused = _pause_check()
    if paused:
        return paused
    if button not in ("left", "right"):
        return {"error": f"bad button: {button}"}
    try:
        from pynput import mouse as pmouse

        sx, sy = _to_desktop(start_x, start_y, monitor)
        ex, ey = _to_desktop(x, y, monitor)
        _send_move_abs(sx, sy)
        m = _mouse()
        m.press(pmouse.Button[button])
        # Interpolated moves: many apps need intermediate motion to track
        # a drag, so walk the line rather than jumping to the end.
        steps = 12
        duration = min(max(0.05, float(duration)), 3.0)
        for i in range(1, steps + 1):
            ix = round(sx + (ex - sx) * i / steps)
            iy = round(sy + (ey - sy) * i / steps)
            _send_move_abs(ix, iy)
            await asyncio.sleep(duration / steps)
        m.release(pmouse.Button[button])
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return _cursor_feedback(
        {
            "dragged": [[int(start_x), int(start_y)], [int(x), int(y)]],
            "monitor": int(monitor), "button": button, "ok": True,
        },
        ex, ey, _resolve_observe(observe),
    )


async def mouse_scroll(
    workspace: str = "", x: int = 0, y: int = 0, monitor: int = 1,
    amount: int = 3, observe: bool | None = None,
) -> dict:
    paused = _pause_check()
    if paused:
        return paused
    try:
        ax, ay = _to_desktop(x, y, monitor)
        _send_move_abs(ax, ay)
        _mouse().scroll(0, int(amount))
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return _cursor_feedback(
        {"scrolled": int(amount), "at": [int(x), int(y)], "monitor": int(monitor), "ok": True},
        ax, ay, _resolve_observe(observe),
    )


async def type_text(workspace: str = "", text: str = "", observe: bool = False) -> dict:
    paused = _pause_check()
    if paused:
        return paused
    try:
        _keyboard().type(text)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return _finish({"typed": len(text), "ok": True}, observe)


async def press_key(
    workspace: str = "", key: str = "", repeat: int = 1, observe: bool = False
) -> dict:
    paused = _pause_check()
    if paused:
        return paused
    parts = [p.strip() for p in (key or "").split("+") if p.strip()]
    if not parts:
        return {"error": "bad key: empty"}
    repeat = min(max(1, int(repeat)), 25)
    try:
        from pynput import keyboard as pkb

        mods = [_parse_key(p, pkb) for p in parts[:-1]]
        last = _parse_key(parts[-1], pkb)
        kb = _keyboard()
        for _ in range(repeat):
            with kb.pressed(*mods):
                kb.tap(last)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return _finish({"pressed": key, "repeat": repeat, "ok": True}, observe)


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
        "mouse_drag": mouse_drag,
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
