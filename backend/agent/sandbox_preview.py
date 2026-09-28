"""Best-effort live preview of the Windows Sandbox client window.

The preview is a native, click-through Win32 window backed by a DWM thumbnail.
It is deliberately independent from sandbox command transport and never moves,
minimizes, or changes the z-order of the real sandbox window.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import threading
import time
import uuid

log = logging.getLogger(__name__)

_PREVIEW_WIDTH = 420
_PREVIEW_HEIGHT = 236
# Default horizontal gap from the yaah GUI's right edge when the preview is
# pinned without a user-established offset (fresh start while pinned, #116).
_PIN_GAP = 24
# EventFireModes / event constants for the SetWinEventHook pin-follow hook.
_EVENT_OBJECT_LOCATIONCHANGE = 0x800B
_WINEVENT_OUTOFCONTEXT = 0x0
_WINEVENT_SKIPOWNTHREAD = 0x2
_DISCOVERY_TIMEOUT = 60.0
_DISCOVERY_INTERVAL = 0.5
_CHECK_INTERVAL = 0.25

_manager_lock = threading.Lock()
_manager: _PreviewManager | None = None


def start_preview() -> bool:
    """Start discovery/rendering asynchronously; never fail sandbox startup."""
    global _manager
    if os.name != "nt" or os.environ.get("YAAH_HEADLESS") == "1":
        return False
    with _manager_lock:
        if _manager is not None and _manager.running:
            # Do not report success for a worker already unwinding after stop;
            # starting a replacement concurrently could leave two thumbnails.
            return not _manager.stopping
        manager = _PreviewManager()
        _manager = manager
        manager.start()
    return True


def stop_preview() -> None:
    """Stop the active preview and release its DWM thumbnail, if any."""
    global _manager
    with _manager_lock:
        manager = _manager
        if manager is None:
            return
        manager.stop()
        # Keep a still-unwinding worker registered so another start cannot
        # accidentally create a duplicate preview during the join timeout.
        if not manager.running and _manager is manager:
            _manager = None


# ---- Pin-to-yaah persistence (#116): `sandbox.preview_pinned` in config.json ----


def get_preview_pinned() -> bool:
    from backend.agent.config import load_config

    return bool((load_config().get("sandbox") or {}).get("preview_pinned"))


def set_preview_pinned(pinned: bool) -> None:
    from backend.agent.config import load_config, save_config

    existing = load_config().get("sandbox") or {}
    save_config({"sandbox": {**existing, "preview_pinned": bool(pinned)}})


# ---- Pure geometry helpers (#116), unit-tested without a display ----


def fit_thumbnail_rect(
    client_w: int, client_h: int, src_w: int, src_h: int
) -> tuple[int, int, int, int]:
    """Letterbox a source image inside a client rect, centered.

    Unknown source size (0) falls back to filling the whole client rect.
    """
    client_w, client_h = max(1, client_w), max(1, client_h)
    if src_w <= 0 or src_h <= 0:
        return 0, 0, client_w, client_h
    scale = min(client_w / src_w, client_h / src_h)
    width, height = max(1, int(src_w * scale)), max(1, int(src_h * scale))
    left, top = (client_w - width) // 2, (client_h - height) // 2
    return left, top, width, height


def pin_offset(
    yaah_rect: tuple[int, int, int, int], prev_rect: tuple[int, int, int, int]
) -> tuple[int, int]:
    """Anchor of the preview relative to the yaah window's right edge:
    preserves whatever horizontal gap the user left, plus vertical offset."""
    _, yaah_y, yaah_r, _ = yaah_rect
    prev_x, prev_y = prev_rect[0], prev_rect[1]
    return prev_x - yaah_r, prev_y - yaah_y


def pinned_position(
    yaah_rect: tuple[int, int, int, int], offset: tuple[int, int]
) -> tuple[int, int]:
    """Preview top-left that reproduces the anchor for a moved yaah window."""
    x, y, right = yaah_rect[0], yaah_rect[1], yaah_rect[2]
    return right + offset[0], y + offset[1]


def _find_yaah_window(user32=None) -> int:
    """HWND of the yaah main window by exact title ("YAAH"); 0 if absent."""
    if user32 is None and (os.name != "nt" or not hasattr(ctypes, "windll")):
        return 0
    user32 = user32 or ctypes.windll.user32
    find = getattr(user32, "FindWindowW")
    if hasattr(ctypes, "WinDLL") and isinstance(user32, ctypes.WinDLL):
        find.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]
        find.restype = ctypes.wintypes.HWND
    return int(find(None, "YAAH"))


class _PreviewManager:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="yaah-sandbox-preview",
            daemon=True,
        )

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                log.warning("sandbox preview thread did not stop promptly")

    def _run(self) -> None:
        try:
            self._run_native()
        except Exception:
            log.exception("sandbox live preview failed")

    def _run_native(self) -> None:
        if not hasattr(ctypes, "windll"):
            return
        user32 = ctypes.windll.user32
        source = _find_sandbox_window(user32)
        deadline = time.monotonic() + _DISCOVERY_TIMEOUT
        while not source and not self._stop.is_set() and time.monotonic() < deadline:
            self._stop.wait(_DISCOVERY_INTERVAL)
            source = _find_sandbox_window(user32)
        if self._stop.is_set() or not source:
            if not source:
                log.info("sandbox preview skipped: client window was not found")
            return
        self._show_preview(user32, ctypes.windll.kernel32, ctypes.windll.dwmapi, source)

    def _show_preview(self, user32, kernel32, dwmapi, source: int) -> None:
        wintypes = ctypes.wintypes
        LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        # SetWinEventHook callback (pin-follow #116): out-of-context, no DLL.
        WINEVENTPROC = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,  # hHook
            wintypes.DWORD,  # event
            wintypes.HWND,  # hwnd
            ctypes.c_long,  # idObject
            ctypes.c_long,  # idChild
            wintypes.DWORD,  # dwEventThread
            wintypes.DWORD,  # dwmsEventTime
        )

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", POINT),
            ]

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON),
            ]

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class SIZE(ctypes.Structure):
            _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

        class THUMBNAIL_PROPERTIES(ctypes.Structure):
            _fields_ = [
                ("dwFlags", wintypes.DWORD),
                ("rcDestination", RECT),
                ("rcSource", RECT),
                ("opacity", ctypes.c_ubyte),
                ("fVisible", wintypes.BOOL),
                ("fSourceClientAreaOnly", wintypes.BOOL),
            ]

        # ctypes must know pointer-sized signatures on 64-bit Windows or HWNDs
        # and the thumbnail handle can be silently truncated.
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        user32.RegisterClassExW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.DefWindowProcW.restype = LRESULT
        user32.PeekMessageW.argtypes = [
            ctypes.POINTER(MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.PeekMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetSystemMenu.argtypes = [wintypes.HWND, wintypes.BOOL]
        user32.GetSystemMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_size_t,
            wintypes.LPCWSTR,
        ]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.CheckMenuItem.argtypes = [wintypes.HMENU, ctypes.c_size_t, wintypes.UINT]
        user32.CheckMenuItem.restype = wintypes.DWORD
        user32.SetWinEventHook.argtypes = [
            wintypes.DWORD,  # eventMin
            wintypes.DWORD,  # eventMax
            wintypes.HMODULE,  # hmodWinEventProc
            WINEVENTPROC,  # pfnWinEventProc
            wintypes.DWORD,  # idProcess
            wintypes.DWORD,  # idThread
            wintypes.DWORD,  # dwFlags
        ]
        user32.SetWinEventHook.restype = wintypes.HANDLE
        user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        user32.UnhookWinEvent.restype = wintypes.BOOL

        dwmapi.DwmRegisterThumbnail.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        dwmapi.DwmRegisterThumbnail.restype = ctypes.c_long
        dwmapi.DwmUnregisterThumbnail.argtypes = [wintypes.HANDLE]
        dwmapi.DwmUnregisterThumbnail.restype = ctypes.c_long
        dwmapi.DwmQueryThumbnailSourceSize.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(SIZE),
        ]
        dwmapi.DwmQueryThumbnailSourceSize.restype = ctypes.c_long
        dwmapi.DwmUpdateThumbnailProperties.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(THUMBNAIL_PROPERTIES),
        ]
        dwmapi.DwmUpdateThumbnailProperties.restype = ctypes.c_long

        WM_SIZE = 0x0005
        WM_SYSCOMMAND = 0x0112
        WM_EXITSIZEMOVE = 0x0232
        MF_STRING = 0x0
        MF_CHECKED = 0x8
        MF_UNCHECKED = 0x0
        MF_BYCOMMAND = 0x0
        # App-command id for the "Pin to yaah" system-menu toggle (#116).
        SC_PIN = 0x7100
        OBJID_WINDOW = 0

        class_name = f"YAAHSandboxPreview_{uuid.uuid4().hex}"
        instance = kernel32.GetModuleHandleW(None)
        thumbnail = wintypes.HANDLE()
        hwnd = None
        class_registered = False
        state = {"pinned": False, "offset": (_PIN_GAP, 24), "hook": None}

        def _client_size() -> tuple[int, int]:
            rect = RECT()
            if hwnd and user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return max(1, rect.right - rect.left), max(1, rect.bottom - rect.top)
            return _PREVIEW_WIDTH, _PREVIEW_HEIGHT

        def _update_thumbnail() -> bool:
            if not thumbnail.value:
                return True
            client_w, client_h = _client_size()
            size = SIZE()
            if dwmapi.DwmQueryThumbnailSourceSize(thumbnail, ctypes.byref(size)) != 0:
                src_w, src_h = 0, 0
            else:
                src_w, src_h = size.cx, size.cy
            left, top, width, height = fit_thumbnail_rect(
                client_w, client_h, src_w, src_h
            )
            props = THUMBNAIL_PROPERTIES()
            props.dwFlags = 0x1 | 0x4 | 0x8  # destination, opacity, visible
            props.rcDestination = RECT(left, top, left + width, top + height)
            props.opacity = 255
            props.fVisible = True
            result = dwmapi.DwmUpdateThumbnailProperties(thumbnail, ctypes.byref(props))
            if result != 0:
                log.warning(
                    "DwmUpdateThumbnailProperties failed with HRESULT 0x%08x",
                    result & 0xFFFFFFFF,
                )
                return False
            return True

        def _yaah_rect() -> tuple[int, int, int, int] | None:
            yaah = _find_yaah_window(user32)
            if not yaah or not user32.IsWindow(yaah):
                return None
            rect = RECT()
            if not user32.GetWindowRect(yaah, ctypes.byref(rect)):
                return None
            return (rect.left, rect.top, rect.right, rect.bottom)

        def _apply_pin() -> None:
            """Move the preview to the yaah anchor (pinned mode only)."""
            if not state["pinned"] or not hwnd:
                return
            yaah = _yaah_rect()
            if yaah is None:
                return
            px, py = pinned_position(yaah, state["offset"])
            user32.SetWindowPos(
                hwnd,
                wintypes.HWND(-1),  # HWND_TOPMOST
                px,
                py,
                0,
                0,
                0x0001 | 0x0004 | 0x0010,  # NOSIZE | NOZORDER | NOACTIVATE
            )

        def _win_event_proc(_hook, _event, event_hwnd, id_object, _child, _t1, _t2):
            # Only the yaah main window's window-object moves matter.
            if (
                state["pinned"]
                and id_object == OBJID_WINDOW
                and _find_yaah_window(user32) == int(event_hwnd or 0)
            ):
                _apply_pin()

        win_event_proc = WINEVENTPROC(_win_event_proc)

        def _set_pinned(pinned: bool) -> None:
            try:
                state["pinned"] = pinned
                if hwnd:
                    yaah = _yaah_rect()
                    prev = RECT()
                    if user32.GetWindowRect(hwnd, ctypes.byref(prev)):
                        prev_rect = (prev.left, prev.top, prev.right, prev.bottom)
                        if pinned and yaah is not None:
                            state["offset"] = pin_offset(yaah, prev_rect)
                        if pinned:
                            _apply_pin()
                if pinned:
                    hook = user32.SetWinEventHook(
                        _EVENT_OBJECT_LOCATIONCHANGE,
                        _EVENT_OBJECT_LOCATIONCHANGE,
                        None,
                        win_event_proc,
                        0,
                        0,
                        _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNTHREAD,
                    )
                    if hook:
                        state["hook"] = hook
                    else:
                        log.warning("could not install pin-follow WinEvent hook")
                else:
                    hook, state["hook"] = state["hook"], None
                    if hook:
                        user32.UnhookWinEvent(hook)
            finally:
                # Persistence must survive any follow-related failure so the
                # toggle never lies about the saved preference.
                try:
                    set_preview_pinned(pinned)
                except Exception:
                    log.warning("could not persist preview pin state", exc_info=True)

        @WNDPROC
        def wnd_proc(window, message, wparam, lparam):
            if message == WM_SIZE:
                _update_thumbnail()
                return 0
            if message == WM_SYSCOMMAND and (wparam & 0xFFF0) == SC_PIN:
                _set_pinned(not state["pinned"])
                return 0
            if message == WM_EXITSIZEMOVE and state["pinned"]:
                # Drag/resize finished: re-anchor to wherever the user left it.
                yaah = _yaah_rect()
                prev = RECT()
                if yaah is not None and user32.GetWindowRect(hwnd, ctypes.byref(prev)):
                    state["offset"] = pin_offset(
                        yaah, (prev.left, prev.top, prev.right, prev.bottom)
                    )
                _apply_pin()
                return 0
            return user32.DefWindowProcW(window, message, wparam, lparam)

        try:
            wnd_class = WNDCLASSEXW()
            wnd_class.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wnd_class.lpfnWndProc = wnd_proc
            wnd_class.hInstance = instance
            wnd_class.lpszClassName = class_name
            if not user32.RegisterClassExW(ctypes.byref(wnd_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            class_registered = True

            # Draggable + resizeable (#116): caption (title bar) + thickframe
            # (resize borders) + system menu. Still WS_EX_TOPMOST and a
            # tool window (no taskbar entry); no longer click-through, so the
            # user can move, resize, and reach the system menu.
            ex_style = 0x00000008 | 0x00000080  # WS_EX_TOPMOST | WS_EX_TOOLWINDOW
            # WS_OVERLAPPEDWINDOW minus WS_MAXIMIZEBOX (thumbnail letterboxes,
            # maximizing adds nothing).
            style = 0x00C00000 | 0x00080000 | 0x00040000 | 0x00020000
            hwnd = user32.CreateWindowExW(
                ex_style,
                class_name,
                "YAAH Sandbox Preview",
                style,
                0,
                0,
                _PREVIEW_WIDTH,
                _PREVIEW_HEIGHT,
                None,
                None,
                instance,
                None,
            )
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())

            # "Pin to yaah" toggle in the system menu (title-bar icon), #116.
            try:
                menu = user32.GetSystemMenu(hwnd, False)
                if menu:
                    user32.AppendMenuW(menu, MF_STRING, SC_PIN, "Pin to yaah")
            except Exception:
                log.debug("could not add Pin to yaah menu item", exc_info=True)

            if dwmapi.DwmRegisterThumbnail(hwnd, source, ctypes.byref(thumbnail)) != 0:
                raise OSError("DwmRegisterThumbnail failed")

            screen_w = user32.GetSystemMetrics(0)
            x = max(0, screen_w - _PREVIEW_WIDTH - 24)
            y = 24
            try:
                state["pinned"] = get_preview_pinned()
            except Exception:
                state["pinned"] = False
            if state["pinned"]:
                yaah = _yaah_rect()
                if yaah is not None:
                    # Fresh start has no user-established offset: default gap.
                    state["offset"] = (_PIN_GAP, y - yaah[1])
                    px, py = pinned_position(yaah, state["offset"])
                    x, y = px, py
            if not user32.SetWindowPos(
                hwnd,
                wintypes.HWND(-1),
                x,
                y,
                _PREVIEW_WIDTH,
                _PREVIEW_HEIGHT,
                0x0010,
            ):  # SWP_NOACTIVATE
                raise ctypes.WinError(ctypes.get_last_error())
            user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            if not _update_thumbnail():
                return
            if state["pinned"]:
                menu = user32.GetSystemMenu(hwnd, False)
                if menu:
                    user32.CheckMenuItem(menu, SC_PIN, MF_CHECKED | MF_BYCOMMAND)
                _set_pinned(True)

            def _pump_messages() -> bool:
                """Drain the message queue; False when WM_QUIT arrived."""
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0001):
                    if msg.message == 0x0012:  # WM_QUIT
                        return False
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                return True

            msg = MSG()
            next_check = time.monotonic() + _CHECK_INTERVAL
            next_follow = 0.0
            while not self._stop.is_set() and user32.IsWindow(source):
                if not _pump_messages():
                    return
                now = time.monotonic()
                if state["pinned"] and now >= next_follow:
                    # Guaranteed pin-follow: a WINEVENT_LOCATIONCHANGE hook
                    # fires only when WinEvents reach this thread's queue,
                    # which is not guaranteed for a Python message loop; a
                    # throttled re-anchor piggybacking this loop keeps follow
                    # reliable at negligible cost (one GetWindowRect per tick).
                    _apply_pin()
                    next_follow = now + 0.06
                if now >= next_check:
                    if not user32.IsWindow(source):
                        break
                    if not _update_thumbnail():
                        break
                    next_check = now + _CHECK_INTERVAL
                self._stop.wait(0.05)
        finally:
            hook = state.get("hook")
            if hook:
                try:
                    user32.UnhookWinEvent(hook)
                except Exception:
                    log.debug("could not unhook pin-follow event", exc_info=True)
            if thumbnail.value:
                try:
                    dwmapi.DwmUnregisterThumbnail(thumbnail)
                except Exception:
                    log.debug(
                        "could not unregister sandbox DWM thumbnail", exc_info=True
                    )
            if hwnd:
                try:
                    if user32.IsWindow(hwnd):
                        user32.DestroyWindow(hwnd)
                except Exception:
                    log.debug("could not destroy sandbox preview window", exc_info=True)
            if class_registered:
                try:
                    user32.UnregisterClassW(class_name, instance)
                except Exception:
                    log.debug(
                        "could not unregister sandbox preview class", exc_info=True
                    )

def _select_main_window(candidates: list[tuple[int, int]]) -> int:
    """Select the largest HWND by bounding-box area; ties prefer first found."""
    if not candidates:
        return 0
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _find_sandbox_window(user32=None) -> int:
    """Return the largest visible WindowsSandboxClient.exe top-level HWND.

    Windows Sandbox may expose multiple top-level windows from its client
    process. EnumWindows order is not a reliable way to distinguish the main
    desktop surface from a narrow title-bar/auxiliary window, so choose the
    candidate with the largest window area.
    """
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        return 0
    user32 = user32 or ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    wintypes = ctypes.wintypes
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    found: list[tuple[int, int]] = []

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]

    def _callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if not process:
            return True
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if (
                kernel32.QueryFullProcessImageNameW(process, 0, buf, ctypes.byref(size))
                and buf.value.replace("\\", "/").rsplit("/", 1)[-1].lower()
                == "windowssandboxclient.exe"
            ):
                rect = wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    width = max(0, rect.right - rect.left)
                    height = max(0, rect.bottom - rect.top)
                    found.append((width * height, int(hwnd)))
        finally:
            kernel32.CloseHandle(process)
        return True

    user32.EnumWindows(callback_type(_callback), 0)
    return _select_main_window(found)
