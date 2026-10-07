"""#339: host-side computer use is retired — no low-level input hooks.

YAAH's backend used to install WH_MOUSE_LL / WH_KEYBOARD_LL activity hooks
plus a pynput global keyboard listener (the panic hotkey) for the lifetime
of the process. When a pytest run saturated the cores, the hook-hosting
thread starved and the cursor hitched system-wide (#299) — only in YAAH,
because no other project runs a hook-hosting process while its tests
execute. The whole surface is gone: no backend.agent.computer module, no
host desktop tools in the registry, no startup seeder, no hook machinery
anywhere in backend source. (There is no public API to enumerate another
process's hooks, so the guard here is the import graph plus a source
sweep; if anything reintroduces pynput/hooks, these tests trip.)
"""

import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

_BANNED_MODULES = ("pynput", "mss", "uiautomation", "comtypes")
_BANNED_NAMES = {
    "screenshot", "list_windows", "read_ui_tree", "focus_window",
    "mouse_move", "mouse_click", "mouse_drag", "mouse_scroll",
    "type_text", "press_key", "wait",
}
_BANNED_SOURCE = (
    "SetWindowsHookExW", "WH_MOUSE_LL", "WH_KEYBOARD_LL", "pynput",
    "start_panic_hotkey", "start_activity_hooks",
)


def test_computer_module_is_gone():
    assert importlib.util.find_spec("backend.agent.computer") is None


def test_no_hook_libraries_loaded():
    """Nothing in the backend import graph may pull an input-hook library
    into the test session (the whole suite runs under this interpreter)."""
    for mod in _BANNED_MODULES:
        assert mod not in sys.modules, mod


def test_no_host_desktop_tools_in_registry():
    from backend.agent import tools

    schema_names = {s["function"]["name"] for s in tools.TOOLS_SCHEMA}
    assert not schema_names & _BANNED_NAMES
    assert not set(tools.EXECUTORS) & _BANNED_NAMES
    assert not set(tools.HELP_DOCS) & _BANNED_NAMES


def test_no_hook_machinery_in_backend_source():
    """Source sweep: the LL-hook / pynput machinery must never return to
    the shipped backend (vendored windows-mcp inside the sandbox toolkit
    is exempt — it runs in the VM, never on the host)."""
    for path in BACKEND.rglob("*.py"):
        if "bundled_toolkit" in path.parts or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for banned in _BANNED_SOURCE:
            assert banned not in text, (path.name, banned)


def test_backend_main_has_no_startup_seeder():
    src = (BACKEND / "main.py").read_text(encoding="utf-8")
    assert "start_background" not in src
    assert "import computer" not in src
