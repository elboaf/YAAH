"""GUI front-ends for the YAAH Windows server (issue #466): the install
wizard, the Programs & Features uninstaller, and the status window.

Tkinter only — it ships with the CPython build PyInstaller bundles, so the
artifact gains no new dependency (tcl/tk is collected automatically). The
pure decision helpers (config pre-fill, field validation, the uninstall
plan) sit above the tkinter import points and are covered by headless
tests; each run_gui_* imports tkinter lazily inside the function, so
importing this module never requires a display.

`se` is a namespace of server_entry callables/constants (server_entry
builds it via _gui_api()): scripts/ is not a package and the frozen exe
runs server_entry as __main__, so server_gui never imports it back —
every external effect (service, registry, firewall, shortcuts) goes
through `se`, keeping this module UI-only.

The install/uninstall run in a worker thread and report through a queue
the tkinter main loop pumps with root.after — a blocking step would
freeze the window ("not responding").
"""

import json
import os
import sys

DEFAULT_PORT = 8765
DEFAULT_HOST = "0.0.0.0"


# ---------------------------------------------------------------- pure helpers

def load_existing_remote(profile_dir: str | None) -> dict:
    """The `remote` block of <profile>/.yaah/config.json, {} when absent.

    Upgrades pre-fill the wizard from this so an existing install never
    retypes the passphrase. Best-effort: any read/parse problem is {}.
    """
    if not profile_dir:
        return {}
    path = os.path.join(os.path.normpath(profile_dir), ".yaah", "config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    remote = cfg.get("remote")
    return remote if isinstance(remote, dict) else {}


def validate_install_fields(passphrase, display, port_text, host):
    """Validate wizard input; returns (errors, normalized values).

    The empty host means the default LAN bind; port must be a number in
    1-65535 (the desktop app's direct-IP connect assumes 8765 but any
    legal port serves).
    """
    errors: list[str] = []
    passphrase = (passphrase or "").strip()
    display = (display or "").strip()
    host = (host or "").strip() or DEFAULT_HOST
    if not passphrase:
        errors.append("a passphrase is required (without one the server "
                      "refuses all remote access)")
    port = DEFAULT_PORT
    if (port_text or "").strip():
        try:
            port = int(port_text.strip())
        except ValueError:
            errors.append(f"port must be a number (default {DEFAULT_PORT})")
        else:
            if not 1 <= port <= 65535:
                errors.append("port must be between 1 and 65535")
    values = {"passphrase": passphrase, "display": display,
              "host": host, "port": port}
    return errors, values


def plan_uninstall(remove_service, remove_config, remove_exe, *,
                   profile_dir, installed) -> list[str]:
    """The exact deletion summary shown before the confirm dialog.

    The Programs & Features entry and Start Menu shortcuts are tied to
    the service removal (they describe the install); the data folder is
    shared with the desktop app on this box, so leaving it is spelled
    out, not silent.
    """
    lines: list[str] = []
    if remove_service:
        lines.append("stop and remove the YaahServer service (+ firewall rules)")
        lines.append("remove the Programs & Features entry and Start Menu shortcuts")
    else:
        lines.append("keep the service installed and running")
        if remove_exe:
            # The SCM starts the exe by PATH at next boot; deleting the
            # image under a live service breaks that boot - warn before
            # the confirm dialog, not after the damage (CodeRabbit round 2).
            lines.append("warning: the service keeps running from a file "
                         "you are deleting - uncheck one of the two")
    if remove_config:
        lines.append(f"delete YAAH data folder {os.path.normpath(profile_dir)}\\.yaah")
    if remove_exe:
        lines.append(f"delete {installed}")
    kept = []
    if not remove_config:
        kept.append("data folder")
    if not remove_exe:
        kept.append("program file")
    if kept:
        lines.append("kept: " + ", ".join(kept))
    return lines


# ---------------------------------------------------------------- gui plumbing

def _center(win, w, h) -> None:
    """Center a toplevel on the screen (tkinter has no built-in)."""
    win.update_idletasks()
    x = max(0, (win.winfo_screenwidth() - w) // 2)
    y = max(0, (win.winfo_screenheight() - h) // 2)
    win.geometry(f"{w}x{h}+{x}+{y}")


# ---------------------------------------------------------------- installer

def run_gui_installer(se, launch_profile: str | None = None) -> int:
    """The setup wizard (double-click, `setup`): collect + install + confirm.

    Detects an existing install (upgrade banner) and pre-fills the
    passphrase and display name from the existing config so upgrades
    don't retype everything.
    """
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print("Tkinter is not available in this build — cannot show the "
              "setup window")
        return 1

    import queue as queue_mod
    import socket
    import threading

    profile = launch_profile or se._service_profile_dir()         or os.path.expanduser("~")
    existing = load_existing_remote(profile)
    already = bool(se._service_state()) or os.path.exists(se._installed_exe())

    root = tk.Tk()
    root.title("YAAH Server Setup")
    root.resizable(False, False)
    _center(root, 540, 400)
    frm = ttk.Frame(root, padding=16)
    frm.grid(row=0, column=0, sticky="nsew")

    row = 0
    ttk.Label(frm, text="YAAH Headless Server",
              font=("", 12, "bold")).grid(
        row=row, column=0, columnspan=2, sticky="w", pady=(0, 2))
    row += 1
    ttk.Label(frm, text="Serves workspace tools (shell, files, git) to YAAH "
                        "desktop clients on the LAN.").grid(
        row=row, column=0, columnspan=2, sticky="w")
    row += 1
    if already:
        ttk.Label(frm, text="Existing install detected — this will upgrade "
                            "it (settings pre-filled).", foreground="#a80",
                  ).grid(row=row, column=0, columnspan=2, sticky="w",
                         pady=(4, 0))
        row += 1

    passphrase_var = tk.StringVar(value=str(existing.get("passphrase") or ""))
    display_var = tk.StringVar(
        value=str(existing.get("display_name") or socket.gethostname()))
    port_var = tk.StringVar(
        value=str(existing.get("port") or DEFAULT_PORT))
    host_var = tk.StringVar(value=str(existing.get("host") or DEFAULT_HOST))

    def field(label, var, **kw):
        """One label + entry row in the wizard grid."""
        nonlocal row
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w",
                                        pady=4)
        ttk.Entry(frm, textvariable=var, **kw).grid(
            row=row, column=1, sticky="we", pady=4, padx=(8, 0))
        row += 1

    field("Passphrase (desktop clients must present it):", passphrase_var,
          show="*")
    field("Display name in the host switcher:", display_var)
    field("Port (desktop direct-connect assumes 8765):", port_var, width=8)
    field("Host bind:", host_var, width=16)

    status_var = tk.StringVar(value="")
    bar = ttk.Progressbar(frm, mode="indeterminate")
    install_btn = ttk.Button(frm, text="Install", command=lambda: do_install())

    frm.columnconfigure(1, weight=1)
    install_btn.grid(row=row, column=0, columnspan=2, sticky="we", pady=(12, 4))
    row += 1
    bar.grid(row=row, column=0, columnspan=2, sticky="we", pady=2)
    row += 1
    ttk.Label(frm, textvariable=status_var).grid(
        row=row, column=0, columnspan=2, sticky="w")

    def do_install():
        """Validate, then run the shared install in a worker thread."""
        errors, vals = validate_install_fields(
            passphrase_var.get(), display_var.get(), port_var.get(),
            host_var.get())
        if errors:
            messagebox.showerror("YAAH Server Setup", "\n".join(errors),
                                 parent=root)
            return
        install_btn.config(state="disabled")
        bar.start(12)
        q: queue_mod.Queue = queue_mod.Queue()

        def worker():
            """Run the install off the UI thread; report via the queue."""
            try:
                state = se._do_install(
                    passphrase=vals["passphrase"], display=vals["display"],
                    host=vals["host"], port=vals["port"], profile=profile,
                    emit=lambda m: q.put(("log", m)))
                q.put(("done", state))
            except se.InstallError as exc:
                q.put(("fail", str(exc)))
            except Exception as exc:  # noqa: BLE001 — surfaced in the dialog
                q.put(("fail", repr(exc)))

        threading.Thread(target=worker, daemon=True).start()

        def pump():
            """Drain the worker queue into the status label + dialogs."""
            try:
                kind, payload = q.get_nowait()
            except queue_mod.Empty:
                root.after(100, pump)
                return
            if kind == "log":
                status_var.set(str(payload))
                root.after(100, pump)
            elif kind == "done":
                bar.stop()
                status_var.set("installed and running")
                messagebox.showinfo(
                    "YAAH Server",
                    "YAAH Headless Server is installed and running.\n\n"
                    f"Connect from the YAAH desktop app: host switcher "
                    f"(mDNS) or direct IP:{vals['port']} with your "
                    "passphrase.\n\n"
                    "Uninstall later from Programs & Features.",
                    parent=root)
                root.destroy()
            else:
                bar.stop()
                status_var.set("failed")
                messagebox.showerror("YAAH Server Setup",
                                     f"Install failed:\n\n{payload}",
                                     parent=root)
                install_btn.config(state="normal")

        root.after(100, pump)

    root.mainloop()
    return 0


# ---------------------------------------------------------------- uninstaller

def run_gui_uninstaller(se, launch_profile: str | None = None) -> int:
    """The Programs & Features uninstaller: three opt-out questions, a
    deletion summary, a confirm dialog, progress, and a completion report.
    """
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print("Tkinter is not available in this build — cannot show the "
              "uninstall window")
        return 1

    import queue as queue_mod
    import threading

    profile = launch_profile or se._service_profile_dir()         or os.path.expanduser("~")
    installed = se._installed_exe()

    root = tk.Tk()  # before the variables: tk vars need a default root
    # Defaults: service + program go, data stays. ~/.yaah is shared with
    # the desktop app on this box (passphrase, memory, skills), so its
    # removal is opt-in — the summary says exactly what is kept.
    svc_var = tk.BooleanVar(value=True)
    cfg_var = tk.BooleanVar(value=False)
    exe_var = tk.BooleanVar(value=True)

    root.title("Uninstall YAAH Server")
    root.resizable(False, False)
    _center(root, 560, 420)
    frm = ttk.Frame(root, padding=16)
    frm.grid(row=0, column=0, sticky="nsew")

    ttk.Label(frm, text="Uninstall YAAH Headless Server",
              font=("", 12, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

    checks = [
        ("Remove the YaahServer service (stop + deregister)", svc_var),
        ("Remove YAAH config and data (~/.yaah — shared with the "
         "desktop app on this box)", cfg_var),
        (f"Remove the installed program ({installed})", exe_var),
    ]
    # Checkbuttons are built before refresh_summary is defined (Python
    # resolves the name when the widget invokes it, not at construction)
    # - but never invoke() a widget mid-construction: a scripted click
    # (or a fast user) during this loop would hit the not-yet-bound name.
    for i, (text, var) in enumerate(checks, start=1):
        ttk.Checkbutton(frm, text=text, variable=var,
                        command=lambda: refresh_summary()).grid(
            row=i, column=0, columnspan=2, sticky="w", pady=2)

    summary = tk.Text(frm, height=9, wrap="word", state="disabled",
                      background="#f4f4f4", relief="flat")
    summary.grid(row=4, column=0, columnspan=2, sticky="we", pady=(10, 4))

    status_var = tk.StringVar(value="")
    bar = ttk.Progressbar(frm, mode="indeterminate")
    uninstall_btn = ttk.Button(frm, text="Uninstall",
                               command=lambda: do_uninstall())

    frm.columnconfigure(0, weight=1)
    uninstall_btn.grid(row=5, column=0, columnspan=2, sticky="we",
                       pady=(6, 4))
    bar.grid(row=6, column=0, columnspan=2, sticky="we", pady=2)
    ttk.Label(frm, textvariable=status_var).grid(
        row=7, column=0, columnspan=2, sticky="w")

    def refresh_summary():
        """Re-render the deletion summary from the current checkboxes."""
        if summary is None:  # not built yet (no scripted clicks ran)
            return
        lines = plan_uninstall(
            svc_var.get(), cfg_var.get(), exe_var.get(),
            profile_dir=profile, installed=installed)
        summary.config(state="normal")
        summary.delete("1.0", "end")
        summary.insert("1.0", "\n".join("• " + ln for ln in lines))
        summary.config(state="disabled")

    refresh_summary()

    def do_uninstall():
        """Confirm, then run each opted-in phase in a worker thread."""
        if not messagebox.askyesno(
                "Uninstall YAAH Server",
                "Proceed with the removal listed above?\n\n"
                "This cannot be undone.", parent=root):
            return
        uninstall_btn.config(state="disabled")
        bar.start(12)
        remove_svc, remove_cfg, remove_exe = (
            svc_var.get(), cfg_var.get(), exe_var.get())
        q: queue_mod.Queue = queue_mod.Queue()

        def worker():
            """Removal off the UI thread (delegates to _do_uninstall)."""
            # Delegate to server_entry's ONE removal sequence (CodeRabbit
            # #466 round 2): the GUI must not carry its own diverging
            # copy of stop/remove/firewall/ARP/Start Menu/data/exe
            # phases - the same logic the CLI and headless paths run.
            try:
                removed, failed = se._do_uninstall(
                    profile=profile, remove_service=remove_svc,
                    remove_config=remove_cfg, remove_exe=remove_exe,
                    emit=lambda m: q.put(("log", m)))
                q.put(("done", removed, failed))
            except Exception as exc:  # noqa: BLE001 - surfaced in the dialog
                q.put(("fail", repr(exc)))

        threading.Thread(target=worker, daemon=True).start()

        def pump():
            """Drain the worker queue into the status label + dialogs."""
            try:
                kind, *payload = q.get_nowait()
            except queue_mod.Empty:
                root.after(100, pump)
                return
            if kind == "log":
                status_var.set(str(payload[0]))
                root.after(100, pump)
                return
            bar.stop()
            if kind == "fail":
                status_var.set("failed")
                messagebox.showerror(
                    "Uninstall YAAH Server",
                    f"Uninstall failed:\n\n{payload[0]}", parent=root)
                uninstall_btn.config(state="normal")
                return
            removed, skipped = payload
            status_var.set("done")
            removed_lines = [f"• {r}" for r in removed] or ["• nothing"]
            lines = ["Removed:"] + removed_lines
            if skipped:
                lines += ["", "Not removed:"] + [f"• {s}" for s in skipped]
            messagebox.showinfo("Uninstall YAAH Server", "\n".join(lines),
                                parent=root)
            root.destroy()

        root.after(100, pump)

    root.mainloop()
    return 0


# ---------------------------------------------------------------- status

def run_gui_status(se, launch_profile: str | None = None) -> int:
    """Small status window: service state, bind, version, last start time."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        print("Tkinter is not available in this build — cannot show the "
              "status window")
        return 1

    import os as os_mod

    root = tk.Tk()
    root.title("YAAH Server Status")
    root.resizable(False, False)
    _center(root, 420, 260)
    frm = ttk.Frame(root, padding=14)
    frm.grid(row=0, column=0, sticky="nsew")
    frm.columnconfigure(1, weight=1)

    state_var = tk.StringVar(value="…")
    bind_var = tk.StringVar(value="…")
    display_var = tk.StringVar(value="…")
    version_var = tk.StringVar(value="…")
    started_var = tk.StringVar(value="…")
    auth_var = tk.StringVar(value="…")

    rows = [("Service", state_var), ("Bind", bind_var),
            ("Display name", display_var), ("Version", version_var),
            ("Last start", started_var), ("Passphrase", auth_var)]
    for i, (label, var) in enumerate(rows):
        ttk.Label(frm, text=label + ":").grid(row=i, column=0, sticky="w",
                                              pady=2)
        ttk.Label(frm, textvariable=var).grid(row=i, column=1, sticky="w",
                                              pady=2, padx=(12, 0))

    btns = ttk.Frame(frm)
    btns.grid(row=len(rows), column=0, columnspan=2, sticky="we",
              pady=(12, 0))
    alive = {"on": True}

    def refresh(full=True):
        """Update the labels; full also re-queries the process start time
        (a PowerShell call — too slow for the 3 s auto-tick)."""
        if not alive["on"]:
            return
        raw = se._service_state()
        if not raw:
            state_var.set("not installed")
        elif "RUNNING" in raw:
            state_var.set("running")
        elif "STOPPED" in raw:
            state_var.set("stopped")
        else:
            state_var.set(raw)
        host, port = se._configured_bind()
        bind_var.set(f"{host}:{port}")
        remote = load_existing_remote(os_mod.path.expanduser("~"))
        display_var.set(str(remote.get("display_name") or "—"))
        version_var.set(se._display_version())
        if full:
            started_var.set(se._service_started_at())
        auth_var.set("set" if remote.get("passphrase") else "not set")

    def tick():
        """Re-poll the cheap fields every 3s (start time only on Refresh)."""
        if not alive["on"]:
            return
        refresh(full=False)
        root.after(3000, tick)

    ttk.Button(btns, text="Refresh", command=lambda: refresh(full=True)).grid(
        row=0, column=0, padx=(0, 6))
    ttk.Button(btns, text="Open Logs",
               command=lambda: se._open_logs()).grid(row=0, column=1,
                                                     padx=(0, 6))
    ttk.Button(btns, text="Close",
               command=lambda: close()).grid(row=0, column=2)

    def close():
        """Stop the auto-tick first, then close (the tick holds after()s)."""
        alive["on"] = False
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    refresh(full=True)
    root.after(3000, tick)
    root.mainloop()
    return 0
