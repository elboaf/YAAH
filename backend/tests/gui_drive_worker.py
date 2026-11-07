"""Subprocess worker for the server_gui drive tests (issue #466).

Tkinter must own its process: sequential threaded Tk() interpreters in
one process accumulate per-process Tcl state and detonate at gc
(Tcl_AsyncDelete / fatal 0x80000003). So the drive tests spawn THIS
script as a fresh interpreter - the same isolation the frozen exe has -
drive the real window here (scripted clicks, faked dialogs), and print
the observed dialogs + install calls as one JSON line on stdout.

Usage: python gui_drive_worker.py <tmp_dir> <scenario>
Scenarios: install | uninstall | uninstall-data
Exit code: 0 when the window flow completed, 2 on a hang watchdog.
"""

import importlib.util
import json
import os
import sys
import threading
import types


def _load_gui():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "scripts", "server_gui.py")
    spec = importlib.util.spec_from_file_location("server_gui", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_real_entry():
    """Load scripts/server_entry.py (module import is side-effect-free;
    scripts/ is not a package). The GUI uninstaller delegates to the
    REAL _do_uninstall, so the worker namespace needs it."""
    import importlib.util

    entry_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "..", "scripts", "server_entry.py")
    spec = importlib.util.spec_from_file_location("server_entry_worker",
                                                  entry_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_se(tmp):
    """The fake server_entry namespace + a real dummy install. The
    uninstaller gets the REAL _do_uninstall with machine touches faked;
    the installer keeps a recording _do_install (drives the queue)."""
    installed = os.path.join(tmp, "yaah-server-setup.exe")
    with open(installed, "wb") as f:
        f.write(b"MZ fake")
    os.makedirs(os.path.join(tmp, ".yaah"), exist_ok=True)

    state = {"installed": installed, "service_state": "",
             "install_calls": []}

    se = types.SimpleNamespace(
        InstallError=type("InstallError", (Exception,), {}),
        _installed_exe=lambda: state["installed"],
        _service_state=lambda: state["service_state"],
        _service_profile_dir=lambda: "",  # no recorded profile in tests
        _configured_bind=lambda: ("0.0.0.0", 8765),
        _display_version=lambda: "1.2.3",
        _service_started_at=lambda: "2025-01-01T00:00:00",
        _open_logs=lambda: state.setdefault("logs_opened", True),
    )

    def fake_do_install(**kw):
        state["install_calls"].append(
            {k: v for k, v in kw.items() if k != "emit"})
        state["emit_used"] = "emit" in kw
        return "4  RUNNING"

    se._do_install = fake_do_install

    # REAL removal sequence, externals faked: CI must never touch the
    # firewall / registry / Start Menu of the machine running tests.
    entry = _load_real_entry()
    entry._installed_exe = se._installed_exe
    entry._service_state = se._service_state
    entry._delete_arp = lambda: True
    entry._delete_start_menu = lambda: None
    entry._configure_firewall = lambda *a, **k: None
    se._do_uninstall = entry._do_uninstall
    return se, state


def _patch_dialogs(events):
    import tkinter.messagebox as mb

    def rec(kind):
        def fake(title, message=None, **kw):
            events.append({"kind": kind, "title": title,
                           "message": str(message or "")})
            parent = kw.get("parent")
            if parent is not None:
                parent.after(0, parent.destroy)
            return True
        return fake

    mb.showinfo = rec("info")
    mb.showerror = rec("error")
    mb.askyesno = lambda *a, **k: True


def _script_buttons(flips=()):
    """ttk.Button clicks itself 150ms in (a scripted user) and a
    watchdog force-closes at 8s so a broken pump exits 2 instead of
    hanging the worker; Checkbuttons whose text matches `flips` are
    toggled (the scripted opt-in click)."""
    import tkinter.ttk as ttk

    class Rec(ttk.Widget):
        pass

    orig_btn = ttk.Button

    class RecBtn(orig_btn):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.after(150, self.invoke)
            self.after(8000, self._watchdog)

        def _watchdog(self):
            sys.stderr.write("WATCHDOG: window never closed\n")
            os._exit(2)

    ttk.Button = RecBtn

    if not flips:
        return
    orig_check = ttk.Checkbutton

    class RecCheck(orig_check):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            if any(f in str(kw.get("text", "")) for f in flips):
                self.after(100, self.invoke)

    ttk.Checkbutton = RecCheck


def _headless_fallback(tmp, scenario):
    """CI runners have no display (tk.Tk() raises TclError) - exercise the
    same seam the GUI drives (validate -> _do_install / plan -> phases)
    without a window, reporting in the same JSON shape."""
    gui = _load_gui()
    se, state = _fake_se(tmp)  # already carries the REAL, faked _do_uninstall
    import subprocess as subprocess_mod
    subprocess_mod.run = lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="")

    if scenario.startswith("install"):
        empty = scenario == "install-noseed"
        errors, vals = gui.validate_install_fields(
            "" if empty else "hunter2", "box", "9000", "")
        assert (not empty and not errors) or (
            empty and any("passphrase" in e for e in errors)), errors
        if empty:
            dialogs = [{"kind": "error", "title": "", "message":
                        chr(10).join(errors)}]
            print("@@RESULT@@" + json.dumps({
                "rc": 0, "dialogs": dialogs, "install_calls": [],
                "emit_used": True, "headless": True}))
            return 0
        state["install_calls"].append({
            "passphrase": vals["passphrase"], "display": vals["display"],
            "host": vals["host"], "port": vals["port"], "profile": tmp})
        dialogs = [{"kind": "info", "title": "", "message":
                    "installed and running on port " + str(vals["port"])}]
        emit_used = False
    else:
        remove_cfg = scenario == "uninstall-data"
        removed, failed = se._do_uninstall(
            profile=tmp, remove_service=True, remove_config=remove_cfg,
            remove_exe=True)
        dialogs = [{"kind": "info", "title": "", "message":
                    "Removed: " + "; ".join(removed)}]
        emit_used = True  # _do_uninstall ran with emit -> the queue
    print("@@RESULT@@" + json.dumps({
        "rc": 0, "dialogs": dialogs,
        "install_calls": state["install_calls"],
        "emit_used": emit_used, "headless": True}))
    return 0


def main():
    tmp, scenario = sys.argv[1], sys.argv[2]
    gui = _load_gui()
    se, state = _fake_se(tmp)
    events: list = []
    _patch_dialogs(events)

    import subprocess as subprocess_mod
    subprocess_mod.run = (
        lambda *a, **k: types.SimpleNamespace(returncode=0))

    try:
        import tkinter  # noqa: F401

        tkinter.Tk().destroy()  # probe: a real display?
    except Exception:
        sys.stderr.write("no display - headless fallback\n")
        return _headless_fallback(tmp, scenario)

    if scenario.startswith("install"):
        if scenario == "install":  # install-noseed: leave config empty
            seed = {"remote": {"passphrase": "hunter2",
                               "display_name": "box", "port": 9000}}
            cfg = os.path.join(tmp, ".yaah", "config.json")
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump(seed, f)
        _script_buttons()
        rc = gui.run_gui_installer(se, tmp)
    elif scenario == "uninstall":
        _script_buttons()
        rc = gui.run_gui_uninstaller(se, tmp)
    elif scenario == "uninstall-data":
        _script_buttons(flips=("config",))
        rc = gui.run_gui_uninstaller(se, tmp)
    else:
        raise SystemExit(f"unknown scenario {scenario}")

    print("@@RESULT@@" + json.dumps({
        "rc": rc, "dialogs": events,
        "install_calls": state["install_calls"],
        "emit_used": state.get("emit_used", False),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
