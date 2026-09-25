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
        user32.SetLayeredWindowAttributes.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_ubyte,
            wintypes.DWORD,
        ]
        user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]

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

        class_name = f"YAAHSandboxPreview_{uuid.uuid4().hex}"
        instance = kernel32.GetModuleHandleW(None)
        thumbnail = wintypes.HANDLE()
        hwnd = None
        class_registered = False

        def _update_thumbnail() -> bool:
            if not thumbnail.value:
                return True
            size = SIZE()
            if dwmapi.DwmQueryThumbnailSourceSize(thumbnail, ctypes.byref(size)) != 0:
                src_w, src_h = _PREVIEW_WIDTH, _PREVIEW_HEIGHT
            else:
                src_w, src_h = max(1, size.cx), max(1, size.cy)
            scale = min(_PREVIEW_WIDTH / src_w, _PREVIEW_HEIGHT / src_h)
            width, height = max(1, int(src_w * scale)), max(1, int(src_h * scale))
            left, top = (_PREVIEW_WIDTH - width) // 2, (_PREVIEW_HEIGHT - height) // 2
            props = THUMBNAIL_PROPERTIES()
            props.dwFlags = 0x1 | 0x4 | 0x8  # destination, opacity, visible
            props.rcDestination = RECT(left, top, left + width, top + height)
            props.opacity = 255
            props.fVisible = True
            result = dwmapi.DwmUpdateThumbnailProperties(
                thumbnail, ctypes.byref(props)
            )
            if result != 0:
                log.warning(
                    "DwmUpdateThumbnailProperties failed with HRESULT 0x%08x",
                    result & 0xFFFFFFFF,
                )
                return False
            return True

        @WNDPROC
        def wnd_proc(window, message, wparam, lparam):
            if message == 0x0005:  # WM_SIZE
                _update_thumbnail()
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

            # WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE |
            # WS_EX_TRANSPARENT | WS_EX_LAYERED. The layered+transparent
            # combination makes the preview pass mouse input through.
            ex_style = 0x00000008 | 0x00000080 | 0x08000000 | 0x00000020 | 0x00080000
            # WS_POPUP; no caption or taskbar entry.
            hwnd = user32.CreateWindowExW(
                ex_style,
                class_name,
                "YAAH Sandbox Preview",
                0x80000000,
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
            user32.SetLayeredWindowAttributes.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_ubyte,
                wintypes.DWORD,
            ]
            user32.SetLayeredWindowAttributes(hwnd, 0, 255, 0x2)  # LWA_ALPHA
            if dwmapi.DwmRegisterThumbnail(hwnd, source, ctypes.byref(thumbnail)) != 0:
                raise OSError("DwmRegisterThumbnail failed")

            screen_w = user32.GetSystemMetrics(0)
            x = max(0, screen_w - _PREVIEW_WIDTH - 24)
            if not user32.SetWindowPos(
                hwnd,
                wintypes.HWND(-1),
                x,
                24,
                _PREVIEW_WIDTH,
                _PREVIEW_HEIGHT,
                0x0010,
            ):  # SWP_NOACTIVATE
                raise ctypes.WinError(ctypes.get_last_error())
            user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            if not _update_thumbnail():
                return

            msg = MSG()
            next_check = time.monotonic() + _CHECK_INTERVAL
            while not self._stop.is_set() and user32.IsWindow(source):
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0001):
                    if msg.message == 0x0012:  # WM_QUIT
                        return
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                now = time.monotonic()
                if now >= next_check:
                    if not user32.IsWindow(source):
                        break
                    if not _update_thumbnail():
                        break
                    next_check = now + _CHECK_INTERVAL
                self._stop.wait(0.05)
        finally:
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


def _find_sandbox_window(user32=None) -> int:
    """Return the first visible WindowsSandboxClient.exe top-level HWND."""
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        return 0
    user32 = user32 or ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    wintypes = ctypes.wintypes
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    found: list[int] = []

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
                found.append(int(hwnd))
                return False
        finally:
            kernel32.CloseHandle(process)
        return True

    user32.EnumWindows(callback_type(_callback), 0)
    return found[0] if found else 0
