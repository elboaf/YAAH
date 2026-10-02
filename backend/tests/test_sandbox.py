"""Windows Sandbox integration tests.

No test requires a real sandbox or the Windows feature: generation, config,
risk classes and the run protocol are exercised through fakes (YAAH_SANDBOX_EXE
override + fake spawn), so the suite is green on Linux CI too.
"""
import asyncio
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from backend.agent import sandbox as sb
from backend.agent.tools import tool_risk


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Redirect every host-side location + a fake WindowsSandbox.exe and
    give each test a pristine module session."""
    monkeypatch.setenv("YAAH_SANDBOX_EXE", str(tmp_path / "WindowsSandbox.exe"))
    (tmp_path / "WindowsSandbox.exe").write_bytes(b"fake")
    monkeypatch.setattr(sb, "_base_dir", lambda: tmp_path / "sb")
    monkeypatch.setattr(sb, "toolkit_dir", lambda: tmp_path / "toolkit")
    monkeypatch.setattr(sb, "_SESSION", None)
    # Sandbox lifecycle tests must not start real host UI discovery threads.
    monkeypatch.setattr(sb, "_start_preview", lambda: None)
    monkeypatch.setattr(sb, "_stop_preview", lambda: None)
    # A real WindowsSandbox.exe may be live on the dev host (issue #27's
    # crash tests ran one); the adoption branch would hijack these tests,
    # so default to "no VMs running". Tests exercising adoption override.
    monkeypatch.setattr(sb, "_sandbox_pids", list)
    return tmp_path


def _write_done(logs: Path, n: int, exit_code: int = 0, timed_out: bool = False,
                output: str = "ok"):
    """Pretend the in-sandbox runner acknowledged command n."""
    (logs / "output.txt").write_text(output, encoding="utf-8-sig")
    (logs / f"res.{n}.json").write_text(
        json.dumps({"exit_code": exit_code, "timed_out": timed_out}),
        encoding="ascii")
    (logs / f"done.{n}").write_text("1", encoding="ascii")


# ---------------------------------------------------------------- wsb + bootstrap


def test_wsb_maps_toolkit_rw_workspace_and_logon(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    wsb = sb.generate_wsb(Path(r"C:\proj"), Path(r"C:\tk"), Path(r"C:\logs"))
    assert "<ReadOnly>false</ReadOnly>" in wsb
    assert "<HostFolder>C:\\tk</HostFolder>" in wsb
    assert sb.SB_TOOLKIT in wsb
    assert f"{sb.SB_LOGS}\\{sb.BOOTSTRAP_NAME}" in wsb
    assert "<Networking>Enable</Networking>" in wsb
    assert "<MemoryInMB>8192</MemoryInMB>" in wsb


def test_wsb_escapes_xml_specials_in_paths(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    wsb = sb.generate_wsb(Path(r"C:\demo\my&proj <x>"), Path(r"C:\tk"),
                          Path(r"C:\logs"))
    assert "my&amp;proj &lt;x&gt;" in wsb
    assert "my&proj <x>" not in wsb


def test_wsb_honors_config_and_workspace_mapping_toggle(isolated, monkeypatch):
    monkeypatch.setattr(
        sb.config_mod, "load_config",
        lambda: {"sandbox": {"networking": "Disable", "memory_mb": 4096,
                             "map_workspace": False}})
    wsb = sb.generate_wsb(Path(r"C:\proj"), Path(r"C:\tk"), Path(r"C:\logs"))
    assert "<Networking>Disable</Networking>" in wsb
    assert "<MemoryInMB>4096</MemoryInMB>" in wsb
    assert sb.SB_WS not in wsb  # workspace mount suppressed


def test_bootstrap_is_written_into_the_mapped_logs_dir(isolated, monkeypatch):
    """Regression: the LogonCommand runs the bootstrap from the mapped logs
    dir, but it was written to the session dir — which is not mapped into
    the VM at all, so the VM booted with no bootstrap and every handshake
    and command ack timed out."""
    monkeypatch.setattr(sb.config_mod, "load_config", lambda: {"sandbox": {}})
    sdir = isolated / "sb" / "ws-abc"
    _wsb, boot, logs = sb._write_session_files(sdir, Path(r"C:\proj"))
    assert boot == logs / sb.BOOTSTRAP_NAME
    assert boot.exists()
    assert not (sdir / sb.BOOTSTRAP_NAME).exists()
    # The LogonCommand target must be the file we actually wrote.
    wsb_text = (sdir / "sandbox.wsb").read_text(encoding="utf-8")
    assert f"{sb.SB_LOGS}\\{sb.BOOTSTRAP_NAME}" in wsb_text


def test_bootstrap_signals_ready_and_batches(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    script = sb._bootstrap_script()
    assert "yaah-sandbox-ready" in script
    assert sb.SB_TOOLKIT in script  # toolkit on PATH
    # the processed-set guard against re-running old command files
    assert "$processed" in script
    # child commands must inherit the workspace cwd, not system32
    # (Set-Location alone does not move the process cwd)
    assert "[Environment]::CurrentDirectory = $cwd" in script
    assert "$psi.WorkingDirectory = $cwd" in script
    # firewall off at boot, before the ready marker: a Defender allow
    # dialog on a listening port would stall the session otherwise
    assert "netsh advfirewall set allprofiles state off" in script
    assert script.index("netsh advfirewall") < script.index("yaah-sandbox-ready")


def test_bootstrap_drives_hud_state_and_launches_overlay(isolated, monkeypatch):
    """Issue #119: the VM window must show activity even for CLI-only work.
    The bootstrap writes hud.state (booting / running cmd N / idle) and
    launches the detached HUD overlay BEFORE the ready marker, so the window
    is never an opaque 'nothing is happening' box."""
    monkeypatch.setattr(sb.config_mod, "load_config", lambda: {"sandbox": {}})
    script = sb._bootstrap_script()
    # initial booting state, then running/idle around each command
    assert "hud.state" in script
    assert script.index("hud.state") < script.index("yaah-sandbox-ready")
    assert "running" in script and "idle" in script
    # the overlay itself is shipped base64 (no f-string escaping hazards)
    # and started detached so a render problem can never stall commands
    assert "__yaah_hud.ps1" in script
    assert "FromBase64String" in script
    assert "Start-Process" in script
    assert script.index("__yaah_hud.ps1") < script.index("yaah-sandbox-ready")


def test_hud_overlay_is_topmost_with_console_fallback(isolated, monkeypatch):
    """The HUD prefers an always-on-top WinForms overlay; if overlay
    rendering is unavailable (e.g. vGPU auto-disabled), it falls back to a
    visible text-mode console loop reading the same state file."""
    monkeypatch.setattr(sb.config_mod, "load_config", lambda: {"sandbox": {}})
    hud = sb._HUD_SCRIPT
    assert "TopMost" in hud
    assert "System.Windows.Forms" in hud
    assert "hud.state" in hud
    # fallback path: a visible console rendering the same state file
    assert "-NoExit" in hud or "Clear-Host" in hud


def test_clean_logs_removes_hud_state(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config", lambda: {"sandbox": {}})
    logs = isolated / "logs"
    logs.mkdir()
    (logs / "hud.state").write_text("idle", encoding="utf-8")
    sb._clean_logs(logs)
    assert not (logs / "hud.state").exists()


def test_bootstrap_neuters_interactive_git_editor(isolated, monkeypatch):
    """An editor-opening git command (commit without -m, rebase --continue)
    hangs the VM command channel until the host-side timeout: the bootstrap
    must point GIT_EDITOR/EDITOR/VISUAL at a no-op BEFORE the ready marker,
    so an editor-less commit fails fast with 'empty message' instead."""
    monkeypatch.setattr(sb.config_mod, "load_config", lambda: {"sandbox": {}})
    script = sb._bootstrap_script()
    for var in ("GIT_EDITOR", "EDITOR", "VISUAL"):
        assert f"$env:{var}" in script, var
    assert "true.exe" in script
    # the no-op editor must be set before the VM signals readiness
    assert script.index("true.exe") < script.index("yaah-sandbox-ready")


def test_prompt_and_schemas_warn_against_git_editor(isolated, monkeypatch):
    """The nudge must live in the tool text the model actually sees: the
    sandbox prompt section and the bash/powershell schema descriptions."""
    monkeypatch.setattr(sb.config_mod, "load_config", lambda: {"sandbox": {}})
    assert "git must never open its interactive editor" in sb.prompt_section()

    from backend.agent.tools import POWERSHELL_SCHEMA, TOOLS_SCHEMA

    bash = next(s for s in TOOLS_SCHEMA
                if s["function"]["name"] == "bash")
    for desc in (bash["function"]["description"],
                 POWERSHELL_SCHEMA["function"]["description"]):
        assert "git must never open its editor" in desc
        assert "GIT_EDITOR=true" in desc


# ---------------------------------------------------------------- config


def test_toolkit_dir_config_override(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sb.config_mod, "load_config",
        lambda: {"sandbox": {"toolkit_dir": str(tmp_path / "custom")}})
    assert sb.toolkit_dir() == tmp_path / "custom"


def test_toolkit_dir_defaults_to_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert sb.toolkit_dir() == tmp_path / ".yaah" / "toolkit"


def test_disabled_feature_short_circuits_without_spawning(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {"enabled": False}})

    def boom(*a, **k):
        raise AssertionError("sandbox must not spawn when disabled")

    monkeypatch.setattr(sb, "_spawn", boom)
    result = sb.start_sync(str(isolated))
    assert "error" in result
    assert "disabled" in result["error"]


def test_unavailable_feature_returns_nudge(isolated, monkeypatch):
    # Override the fixture's fake exe: feature genuinely absent.
    monkeypatch.setenv("YAAH_SANDBOX_EXE", str(isolated / "missing.exe"))
    result = sb.start_sync(str(isolated))
    assert "error" in result
    # the model must be able to relay enablement instructions verbatim
    assert "Containers-DisposableClientVM" in result["error"]
    assert "Enable-WindowsOptionalFeature" in result["error"]


def test_available_probe_respects_missing_exe(tmp_path, monkeypatch):
    monkeypatch.setenv("YAAH_SANDBOX_EXE", str(tmp_path / "missing.exe"))
    assert sb.sandbox_available() is False


# ---------------------------------------------------------------- risk classes


def test_sandbox_risk_classes():
    assert tool_risk("sandbox_status") == "read"
    assert tool_risk("sandbox_test") == "mutating"
    assert tool_risk("sandbox_run") == "shell"
    assert tool_risk("sandbox_stop") == "shell"


def test_sandbox_schemas_registered_on_windows():
    import os as _os

    from backend.agent.tools import EXECUTORS, SCHEMAS

    if _os.name != "nt":
        pytest.skip("sandbox tools only register on Windows")
    for name in ("sandbox_test", "sandbox_run", "sandbox_status",
                 "sandbox_stop"):
        assert name in SCHEMAS
        assert name in EXECUTORS


def test_sandbox_schemas_hidden_for_remote_sessions():
    """Sandbox tools drive the LOCAL machine's VMs: a connected remote host
    (even a Windows one) must not see them — and the local path must list
    each of them exactly once (no double-append)."""
    import os as _os

    if _os.name != "nt":
        pytest.skip("sandbox tools only register on Windows")
    from backend.agent import remote as remote_mod
    from backend.agent.tools import get_schemas

    sandbox_names = {"sandbox_test", "sandbox_run", "sandbox_status",
                     "sandbox_stop"}

    class _Stub:
        windows = True

    local = get_schemas()
    assert sandbox_names <= {s["function"]["name"] for s in local}
    assert len([s for s in local
                if s["function"]["name"] == "sandbox_test"]) == 1

    remote_mod.set_remote(_Stub())
    try:
        names_remote = {s["function"]["name"] for s in get_schemas()}
    finally:
        remote_mod.clear_remote()
    assert not (names_remote & sandbox_names)


# ---------------------------------------------------------------- run protocol


def test_run_without_session_errors(isolated):
    result = sb.run_sync("whoami", 5)
    assert "error" in result
    assert "sandbox_test" in result["error"]


def test_stop_without_session_is_idempotent(isolated):
    first = sb.stop_sync()
    second = sb.stop_sync()
    assert first["stopped"] is False
    assert second["stopped"] is False


def test_successful_start_starts_preview_and_stop_cleans_it(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    preview = []
    monkeypatch.setattr(sb, "_start_preview", lambda: preview.append("start"))
    monkeypatch.setattr(sb, "_stop_preview", lambda: preview.append("stop"))

    def fake_spawn(exe, wsb, logs_path):
        (logs_path / "init.log").write_text("yaah-sandbox-ready",
                                            encoding="utf-8")
        return _FakeProc()

    monkeypatch.setattr(sb, "_spawn", fake_spawn)
    monkeypatch.setattr(sb, "session_dir",
                        lambda ws: isolated / "sb" / "ws-abc")
    result = sb.start_sync("C:\\proj")
    assert result["status"] == "running"
    # Reuse exercises the same preview start seam without spawning again.
    assert sb.start_sync("C:\\proj")["status"] == "running"
    assert preview == ["start", "start"]

    monkeypatch.setattr(sb.subprocess, "run", lambda *a, **k: None)
    stopped = sb.stop_sync()
    assert stopped["stopped"] is True
    assert preview == ["start", "start", "stop"]


def test_start_reuses_live_session_without_respawning(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    spawned = []

    def fake_spawn(exe, wsb, logs_path):
        spawned.append(wsb)
        # the real init.log lands in the mapped logs dir
        (logs_path / "init.log").write_text(
            "[$(date)] yaah-sandbox-ready", encoding="utf-8")
        return _FakeProc()

    monkeypatch.setattr(sb, "_spawn", fake_spawn)
    monkeypatch.setattr(sb, "session_dir",
                        lambda ws: isolated / "sb" / "ws-abc")

    first = sb.start_sync("C:\\proj")
    assert first["status"] == "running"
    assert len(spawned) == 1
    # sandbox_test on the SAME workspace reuses the session: no new spawn,
    # no second VM
    second = sb.start_sync("C:\\proj")
    assert second["status"] == "running"
    assert len(spawned) == 1


class _FakeProc:
    def __init__(self):
        self.pid = 4242
        self._rc = None

    def poll(self):
        return self._rc

    def terminate(self):
        # On POSIX, stop_sync terminates the owned process directly rather
        # than using Windows' taskkill command.
        self._rc = 0


def test_run_round_trip_and_sequence(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    logs = isolated / "sb" / "ws-abc" / "logs"

    def fake_spawn(exe, wsb, logs_path):
        (logs_path / "init.log").write_text("yaah-sandbox-ready",
                                            encoding="utf-8")
        return _FakeProc()

    monkeypatch.setattr(sb, "_spawn", fake_spawn)
    monkeypatch.setattr(sb, "session_dir",
                        lambda ws: isolated / "sb" / "ws-abc")
    monkeypatch.setattr(sb, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sb, "_sandbox_pids", list)

    start = sb.start_sync("C:\\proj")
    assert start["status"] == "running"

    # Pre-ack both commands so the test never polls: the first done-marker
    # is written before run_sync's cmd file (stale acks are harmless —
    # _clean_logs wipes markers only when a NEW sandbox is built, and the
    # fake spawn path here skips that anyway... it does not: start_sync
    # builds files BEFORE spawning, so acks must be re-written per cmd).
    _write_done(logs, 1, output="ok")
    result = sb.run_sync("Get-Location", 10)
    assert result["exit_code"] == 0
    assert result["output"] == "ok"
    assert result["timed_out"] is False
    assert (logs / "cmd.1.ps1").exists()
    assert "Get-Location" in (logs / "cmd.1.ps1").read_text(encoding="utf-8")

    _write_done(logs, 2, output="ok")
    second = sb.run_sync("Get-Date", 10)
    assert second["exit_code"] == 0
    assert (logs / "cmd.2.ps1").exists()  # sequence increments


def test_run_reports_foreign_exit_codes_and_truncation(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    logs = isolated / "sb" / "ws-abc" / "logs"

    def fake_spawn(exe, wsb, logs_path):
        (logs_path / "init.log").write_text("yaah-sandbox-ready",
                                            encoding="utf-8")
        return _FakeProc()

    monkeypatch.setattr(sb, "_spawn", fake_spawn)
    monkeypatch.setattr(sb, "session_dir",
                        lambda ws: isolated / "sb" / "ws-abc")
    monkeypatch.setattr(sb, "POLL_INTERVAL", 0.01)

    sb.start_sync("C:\\proj")
    n = sb._next_seq(logs)
    _write_done(logs, n, exit_code=3, output="x" * 25_000)

    result = sb.run_sync("exit 3", 10)
    assert result["exit_code"] == 3
    assert result["truncated"] is True
    assert len(result["output"]) == sb.MAX_OUTPUT_CHARS


def test_adopt_handshake_uses_deterministic_dir(isolated, monkeypatch):
    """The session dir name must be stable across processes (no salted
    hash) or a sandbox left running across an app restart can never be
    re-adopted."""
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    d1 = sb.session_dir("C:\\proj")
    d2 = sb.session_dir("C:\\proj")
    assert d1 == d2
    assert re.match(r"^[A-Za-z0-9_-]+-[0-9a-f]{10}$", d1.name)


def test_start_adopts_running_sandbox_after_app_restart(isolated, monkeypatch):
    """App restarted (module state empty) while the VM is still up: the
    nonce handshake acks, so start_sync re-attaches instead of spawning a
    second sandbox."""
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    spawned = []
    logs = isolated / "sb" / "ws-abc" / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    def ack_nonce(cmd_path):
        # the in-sandbox bootstrap answering the handshake
        n = cmd_path.name[len("cmd."):-len(".ps1")]
        (logs / f"done.{n}").write_text("1", encoding="ascii")

    monkeypatch.setattr(sb, "_spawn", lambda *a: spawned.append(a))
    monkeypatch.setattr(sb, "session_dir",
                        lambda ws: isolated / "sb" / "ws-abc")
    monkeypatch.setattr(sb, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sb, "_sandbox_pids", lambda: [999])
    preview_calls = []
    monkeypatch.setattr(sb, "_start_preview",
                        lambda: preview_calls.append("start"))
    # ack any command file dropped into the logs dir (the nonce handshake)
    monkeypatch.setattr(sb.Path, "write_text",
                        _ack_on_cmd_write(logs, ack_nonce))

    result = sb.start_sync("C:\\proj")
    assert result["status"] == "running"
    assert result["note"].startswith("re-attached")
    assert spawned == []  # no second VM
    assert preview_calls == ["start"]


def test_start_refuses_second_vm_when_handshake_fails(isolated, monkeypatch):
    """A VM is running but answers nothing (started by hand / maps another
    workspace): start_sync must refuse rather than spawn a second sandbox."""
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    logs = isolated / "sb" / "ws-abc" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sb, "session_dir",
                        lambda ws: isolated / "sb" / "ws-abc")
    monkeypatch.setattr(sb, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sb, "ADOPT_TIMEOUT", 0.05)
    monkeypatch.setattr(sb, "_sandbox_pids", lambda: [999])

    result = sb.start_sync("C:\\proj")
    assert "error" in result
    assert "already running" in result["error"]


def _ack_on_cmd_write(logs: Path, ack_nonce):
    """write_text side door: when the host drops a cmd.<nonce>.ps1, write
    the matching done marker. Returns a function compatible with
    monkeypatching Path.write_text for files inside `logs`."""
    real = sb.Path.write_text

    def fake(self, data, *a, **k):
        real(self, data, *a, **k)
        name = self.name
        if name.startswith("cmd.") and name.endswith(".ps1") \
                and self.parent == logs:
            ack_nonce(self)

    return fake


def test_prompt_section_mentions_toolkit_and_gates(isolated):
    text = sb.prompt_section()
    assert "sandbox_test" in text and "sandbox_run" in text
    assert "toolkit" in text
    assert "ask mode" in text
    assert "host by default" in text
    assert "full suites, builds, Python scripts" in text
    assert "network port" in text
    assert "visual inspection" in text

    description = next(
        tool["function"]["description"]
        for tool in sb.SANDBOX_TOOLS_SCHEMA
        if tool["function"]["name"] == "sandbox_test")
    assert "network ports" in description
    assert "GUI visual checks" in description
    assert "automated tests and validation on the host" in description


def test_prompt_section_carries_clean_image_knowledge(isolated):
    """A clean yaah install has no yaah source to read: the prompt section
    is the only place the model learns the VM is a bare image, where
    toolkit-installed tools land on PATH, and how to shim zipped tools."""
    text = sb.prompt_section()
    assert "CLEAN WINDOWS IMAGE" in text
    assert "toolkit\\bin" in text
    assert "toolkit\\Scripts" in text
    assert "dubious ownership" in text
    assert "GIT_CONFIG_GLOBAL" in text


def test_tool_schema_carries_clean_image_knowledge(isolated):
    """Same constraint for the tool definitions: sandbox_run's description
    must survive without source access."""
    desc = next(
        t["function"]["description"]
        for t in sb.SANDBOX_TOOLS_SCHEMA
        if t["function"]["name"] == "sandbox_run")
    assert "CLEAN WINDOWS IMAGE" in desc
    assert "toolkit\\bin" in desc
    assert "windows-mcp" in desc


def test_prompt_section_carries_windows_mcp_playbook(isolated):
    """The windows-mcp findings must transfer to clean installs: the prompt
    is the only knowledge surface a binary install has. It must leave the
    model zero guessing about how to connect and use the MCP server."""
    text = sb.prompt_section()
    assert "windows-mcp" in text
    assert "windows-mcp-serve.ps1" in text
    assert "mcp-session-id" in text
    assert "notifications/initialized" in text
    assert "Bearer" in text
    assert "trailing slash" in text
    assert "launch_executable" in text
    assert "Snapshot" in text
    # AHK is retired: no playbook left in the prompt.
    assert "AutoHotkey" not in text
    assert "ControlSend" not in text


def test_mcp_hint_matches_real_failure_text():
    """The hint keys on output text (exit codes stay 0 for cmdlet errors)."""
    hint = sb._mcp_hint("ControlSend silently did nothing")
    assert hint is not None
    assert "windows-mcp" in hint
    assert sb._mcp_hint("Get-Date") is None
    assert sb._mcp_hint("") is None
    down = sb._mcp_hint("curl: (7) Failed to connect: Connection refused")
    assert down is not None
    assert "windows-mcp-serve.ps1" in down


def test_dialog_stall_hint_fires_on_timeout_with_no_output():
    """The real stall signature: an unattended interactive installer blocks
    on a dialog until the command times out with nothing on stdout."""
    hint = sb._dialog_stall_hint("", True)
    assert hint is not None
    assert "/quiet" in hint
    assert "taskkill" in hint
    # output present = not a dialog stall (the command was working)
    assert sb._dialog_stall_hint("Installing...", True) is None
    # timed out but produced output = not a stall
    assert sb._dialog_stall_hint("progress 50%", True) is None
    # no timeout = not a stall
    assert sb._dialog_stall_hint("", False) is None


def test_prompt_section_carries_silent_install_rule(isolated):
    """The installer-stall lesson must transfer to clean installs."""
    text = sb.prompt_section()
    assert "NEVER run interactive installers unattended" in text
    assert "/quiet" in text
    assert "--silent" in text


def test_prompt_section_documents_firewall_disable(isolated):
    """Why listening ports never pop the Defender allow dialog."""
    text = sb.prompt_section()
    assert "firewall is disabled at boot" in text


def test_prompt_section_says_dispose_when_done(isolated):
    text = sb.prompt_section()
    assert "sandbox_stop" in text
    assert "Dispose the sandbox when the work is done" in text


def test_missing_command_hint_matches_real_failure_text():
    """The real failure (observed live): CommandNotFoundException renders as
    'X is not recognized as the name of a cmdlet' and arrives with
    exit_code 0 — so the hint must key on output text, never exit code."""
    real = ("git : The term 'git' is not recognized as the name of a "
            "cmdlet, function, script file, or operable program. Check "
            "the spelling of the name...")
    hint = sb._missing_command_hint(real)
    assert hint is not None
    assert "toolkit" in hint
    assert "persists" in hint
    assert sb._missing_command_hint("CommandNotFoundException") is not None
    assert sb._missing_command_hint("git version 2.55.0") is None
    assert sb._missing_command_hint("") is None


def test_sandbox_run_attaches_missing_command_hint(isolated, monkeypatch):
    # run_sync is sync (the executor wraps it in to_thread), so the fake
    # must be a plain function too.
    def fake_run_sync(command, timeout):
        return {"exit_code": 0, "timed_out": False,
                "output": "python : The term 'python' is not recognized "
                          "as the name of a cmdlet"}
    monkeypatch.setattr(sb, "run_sync", fake_run_sync)
    result = asyncio.run(sb.sandbox_run("C:\\proj", "python --version", 10))
    assert "clean Windows image" in result["hint"]
    assert result["exit_code"] == 0

    def fake_run_sync_ok(command, timeout):
        return {"exit_code": 0, "timed_out": False, "output": "ok"}
    monkeypatch.setattr(sb, "run_sync", fake_run_sync_ok)
    result = asyncio.run(sb.sandbox_run("C:\\proj", "Get-Date", 10))
    assert "hint" not in result


def test_status_shape(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    status = asyncio.run(sb.sandbox_status("C:\\proj"))
    assert status["available"] is True
    assert status["enabled"] is True
    assert status["running"] is False
    assert status["toolkit_host"].endswith("toolkit")


# ---------------------------------------------------------------- integration seams


def test_execute_tool_dispatches_sandbox_status(isolated, monkeypatch):
    """The executor must work through the real dispatcher (which passes
    workspace= as a kwarg, like every other tool)."""
    import os as _os

    if _os.name != "nt":
        pytest.skip("sandbox tools only register on Windows")
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    from backend.agent.tools import execute_tool

    result = asyncio.run(
        execute_tool("sandbox_status", {}, "C:\\proj"))
    assert result["available"] is True
    assert result["running"] is False


def test_sandbox_run_executor_validates_args(isolated):
    """The executor's own argument validation is cross-platform (no
    dispatch, no Windows feature needed)."""
    result = asyncio.run(sb.sandbox_run(workspace="C:\\proj", command="   "))
    assert "error" in result
    result = asyncio.run(sb.sandbox_run(
        workspace="C:\\proj", command="dir", timeout_seconds="x"))
    assert "error" in result


def test_system_prompt_offers_sandbox_tools_locally(isolated, monkeypatch):
    import os as _os

    if _os.name != "nt":
        pytest.skip("sandbox tools only register on Windows")
    from backend.agent import loop as loop_mod

    monkeypatch.setattr(loop_mod, "load_config",
                        lambda: {"access_mode": "full"})
    prompt = loop_mod._default_system_prompt("C:\\proj")
    assert "sandbox_test" in prompt
    assert "# Windows Sandbox (for tests that need isolation)" in prompt
    assert "persistent dev toolkit" in prompt
    assert "host by default" in prompt
    assert "network port" in prompt
    assert "visual inspection" in prompt


# ------------------------------------------------- mid-session crash (issue #27)


def _start_session(isolated, monkeypatch, proc):
    def fake_spawn(exe, wsb, logs_path):
        (logs_path / "init.log").write_text("yaah-sandbox-ready",
                                            encoding="utf-8")
        return proc

    monkeypatch.setattr(sb, "_spawn", fake_spawn)
    monkeypatch.setattr(sb, "session_dir",
                        lambda ws: isolated / "sb" / "ws-abc")
    monkeypatch.setattr(sb, "POLL_INTERVAL", 0.01)
    sb.start_sync("C:\\proj")


def test_run_detects_vm_death_as_crash_class(isolated, monkeypatch):
    """Popen exits while a command is outstanding: the error must name the
    crash class (0x80072746), not the old generic closed-channel text."""
    proc = _FakeProc()
    _start_session(isolated, monkeypatch, proc)
    proc._rc = 1  # the VM dies right after start
    result = sb.run_sync("Get-Date", 10)
    assert result.get("crashed") is True
    assert "0x80072746" in result["error"]


def test_run_dead_vm_ack_timeout_is_crash_class(isolated, monkeypatch):
    """Adopted session (no Popen): the VM's PID vanishes from the process
    list while a command is outstanding — crash classification plus a
    diagnostics snapshot in the session logs dir."""
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    logs = isolated / "sb" / "ws-abc" / "logs"
    _start_session(isolated, monkeypatch, _FakeProc())
    (logs / "init.log").write_text("crashed boot", encoding="utf-8")
    # Swap to an adopted-style session: liveness = PID in the process list.
    pids = [999]
    monkeypatch.setattr(sb, "_sandbox_pids", lambda: pids)
    monkeypatch.setattr(sb, "_SESSION", {
        "proc": None, "pid": 999, "dir": logs.parent, "logs": logs,
        "workspace": "C:\\proj", "adopted": True})
    sb._set_last_workspace("C:\\proj")
    pids.clear()  # the VM dies before the command is written
    result = sb.run_sync("Get-Date", 1)
    assert result.get("crashed") is True
    assert "0x80072746" in result["error"]
    assert "crash_diagnostics" in result
    snaps = list(logs.glob("crash-*/init.log"))
    assert snaps and snaps[0].read_text(encoding="utf-8") == "crashed boot"


def test_run_hung_vm_ack_timeout_is_not_crash_class(isolated, monkeypatch):
    """No ack but the VM still listed: the classic hung-channel error (a
    reboot would not help, and must not fire)."""
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    _start_session(isolated, monkeypatch, _FakeProc())
    monkeypatch.setattr(sb, "_sandbox_pids", lambda: [999])  # VM alive
    result = sb.run_sync("Start-Sleep 999", 1)
    assert "crashed" not in result
    assert "session looks dead" in result["error"]


def test_crash_triggers_one_shot_auto_reboot(isolated, monkeypatch):
    """On a detected crash, run_sync transparently re-runs start_sync once
    (state loss is fine for a disposable VM) and reports the reboot."""
    proc = _FakeProc()
    _start_session(isolated, monkeypatch, proc)
    proc._rc = 1
    booted = []

    def fake_start(workspace):
        booted.append(workspace)
        return {"status": "running"}

    monkeypatch.setattr(sb, "start_sync", fake_start)
    result = sb.run_sync("Get-Date", 10)
    assert booted == ["C:\\proj"]
    assert "rebooted automatically" in result["reboot"]


def test_auto_reboot_can_be_disabled(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {"auto_reboot_on_crash": False}})
    proc = _FakeProc()
    _start_session(isolated, monkeypatch, proc)
    proc._rc = 1

    def boom(workspace):
        raise AssertionError("reboot must not fire when disabled")

    monkeypatch.setattr(sb, "start_sync", boom)
    result = sb.run_sync("Get-Date", 10)
    assert result.get("crashed") is True
    assert "reboot" not in result


def test_vgpu_auto_disables_on_multi_gpu_host(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {"vgpu": "auto"}})
    monkeypatch.setattr(sb, "_gpu_adapter_count", lambda: 3)
    assert sb.effective_vgpu() == "Disable"
    monkeypatch.setattr(sb, "_gpu_adapter_count", lambda: 1)
    assert sb.effective_vgpu() == "Default"


def test_vgpu_explicit_setting_wins_over_auto(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {"vgpu": "Disable"}})
    monkeypatch.setattr(sb, "_gpu_adapter_count", lambda: 1)
    assert sb.effective_vgpu() == "Disable"


def test_wsb_uses_effective_vgpu(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {"vgpu": "auto"}})
    monkeypatch.setattr(sb, "_gpu_adapter_count", lambda: 2)
    wsb = sb.generate_wsb(Path(r"C:\proj"), Path(r"C:\tk"), Path(r"C:\logs"))
    assert "<vGPU>Disable</vGPU>" in wsb


def _session_up(isolated, monkeypatch, cfg=None):
    """Start a fake-VM session; returns the logs dir."""
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": cfg or {}})

    def fake_spawn(exe, wsb, logs_path):
        (logs_path / "init.log").write_text("yaah-sandbox-ready",
                                            encoding="utf-8")
        return _FakeProc()

    monkeypatch.setattr(sb, "_spawn", fake_spawn)
    monkeypatch.setattr(sb, "session_dir",
                        lambda ws: isolated / "sb" / "ws-abc")
    monkeypatch.setattr(sb, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sb, "_sandbox_pids", list)
    start = sb.start_sync(r"C:\proj")
    assert start["status"] == "running"
    return isolated / "sb" / "ws-abc" / "logs"


def test_concurrent_run_gets_busy_error_not_interleaving(isolated, monkeypatch):
    """Issue #27 mutex: while one chat's command is in flight (mutex held),
    a second chat's sandbox_run waits busy_wait_seconds and then fails with
    a busy error — it must never interleave on the shared output channel."""
    logs = _session_up(isolated, monkeypatch, {"busy_wait_seconds": 0})

    # Simulate chat A's command mid-flight: hold the mutex like _run_command
    # does for the whole round-trip.
    assert sb._lock.acquire(timeout=1)
    try:
        result = sb.run_sync("echo b", 5)
        assert result.get("busy") is True
        assert "busy" in result["error"]
        assert "sandbox_test" in result["error"]  # anti-pattern warning
        # The refused command never reached the shared channel.
        assert not (logs / "cmd.1.ps1").exists()
    finally:
        sb._lock.release()

    # Once the mutex is free, chat B's command goes through normally.
    _write_done(logs, 1, output="ok")
    result = sb.run_sync("echo b", 5)
    assert result.get("exit_code") == 0
    assert (logs / "cmd.1.ps1").exists()


def test_busy_wait_zero_means_immediate_refusal(isolated, monkeypatch):
    _session_up(isolated, monkeypatch, {"busy_wait_seconds": 0})
    assert sb._lock.acquire(timeout=1)
    try:
        t0 = time.monotonic()
        result = sb.run_sync("echo b", 5)
        assert time.monotonic() - t0 < 1  # no long block on the busy path
    finally:
        sb._lock.release()
    assert result.get("busy") is True


def test_stop_while_busy_defers_instead_of_stranding(isolated, monkeypatch):
    """sandbox_stop during another chat's command doesn't kill the session
    out from under the waiter; it reports busy and leaves the VM alone."""
    logs = _session_up(isolated, monkeypatch, {"busy_wait_seconds": 0})
    assert sb._lock.acquire(timeout=1)
    try:
        result = sb.stop_sync()
        assert result["stopped"] is False
        assert "busy" in result["note"]
        assert sb._alive()  # session untouched
    finally:
        sb._lock.release()


def test_start_sync_while_booting_reports_busy(isolated, monkeypatch):
    """Two chats calling sandbox_test at once: the loser gets the busy
    error, not a second spawned VM."""
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {"busy_wait_seconds": 0}})
    spawned = []

    def slow_spawn(exe, wsb, logs_path):
        spawned.append(wsb)
        time.sleep(0.3)  # boot in progress
        (logs_path / "init.log").write_text("yaah-sandbox-ready",
                                            encoding="utf-8")
        return _FakeProc()

    monkeypatch.setattr(sb, "_spawn", slow_spawn)
    monkeypatch.setattr(sb, "session_dir",
                        lambda ws: isolated / "sb" / "ws-abc")
    monkeypatch.setattr(sb, "_sandbox_pids", list)
    monkeypatch.setattr(sb, "POLL_INTERVAL", 0.01)

    holder = threading.Thread(target=sb.start_sync, args=(r"C:proj",))
    holder.start()
    time.sleep(0.1)  # let the holder take the mutex
    try:
        second = sb.start_sync(r"C:\proj")
        assert second.get("busy") is True
    finally:
        holder.join()
    assert len(spawned) == 1  # never a second VM


# ---------------------------------------------------------------- toolkit seeding


def test_ensure_toolkit_seed_copies_bundled_baseline(isolated, monkeypatch):
    """A fresh install (empty toolkit) gets the shipped baseline: helpers,
    gitconfig, README and the state manifest land before the first boot."""
    src = isolated / "bundled"
    (src / "bin").mkdir(parents=True)
    (src / "bin" / "vm-capture.ps1").write_text("# capture", encoding="utf-8")
    (src / "bin" / "git.cmd").write_text("# shim", encoding="utf-8")
    (src / "gitconfig").write_text("[safe]\n\tdirectory = *\n", encoding="utf-8")
    (src / "README.md").write_text("# toolkit", encoding="utf-8")
    (src / "state.json").write_text(
        json.dumps({"tools": {"vm-capture": {"version": "1.0"}}}),
        encoding="utf-8")
    monkeypatch.setattr(sb, "bundled_toolkit_source", lambda: src)

    written = sb.ensure_toolkit_seed()

    tk = sb.toolkit_dir()
    for rel in ("bin/vm-capture.ps1", "bin/git.cmd", "gitconfig",
                "README.md", "state.json"):
        assert (tk / rel).is_file(), rel
    assert sorted(written) == sorted(["bin/vm-capture.ps1", "bin/git.cmd",
                                      "gitconfig", "README.md", "state.json",
                                      "INDEX.md"])


def test_ensure_toolkit_seed_never_overwrites_user_files(isolated, monkeypatch):
    """Copy-once semantics: a user-modified gitconfig (or any file) survives
    app upgrades and re-seeds untouched."""
    src = isolated / "bundled"
    src.mkdir()
    (src / "gitconfig").write_text("[safe]\n\tdirectory = *\n", encoding="utf-8")
    monkeypatch.setattr(sb, "bundled_toolkit_source", lambda: src)

    tk = sb.toolkit_dir()
    tk.mkdir(parents=True, exist_ok=True)
    (tk / "gitconfig").write_text("# user's own config", encoding="utf-8")

    written = sb.ensure_toolkit_seed()

    assert written == []
    assert (tk / "gitconfig").read_text(encoding="utf-8") == "# user's own config"


def test_ensure_toolkit_seed_merges_state_manifest(isolated, monkeypatch):
    """User-registered tools in state.json survive; bundled entries are
    added; existing entries (possibly version-bumped by a session) win."""
    src = isolated / "bundled"
    src.mkdir()
    (src / "state.json").write_text(json.dumps({"tools": {
        "vm-capture": {"version": "1.0"},
        "new-bundled": {"version": "2.0"},
    }}), encoding="utf-8")
    monkeypatch.setattr(sb, "bundled_toolkit_source", lambda: src)

    tk = sb.toolkit_dir()
    tk.mkdir(parents=True, exist_ok=True)
    (tk / "state.json").write_text(json.dumps({"tools": {
        "vm-capture": {"version": "9.9", "kind": "script"},
        "user-tool": {"version": "0.1"},
    }}), encoding="utf-8")

    sb.ensure_toolkit_seed()

    merged = json.loads((tk / "state.json").read_text(encoding="utf-8"))["tools"]
    assert merged["vm-capture"]["version"] == "9.9"   # existing entry wins
    assert merged["user-tool"]["version"] == "0.1"    # user entry kept
    assert merged["new-bundled"]["version"] == "2.0"  # new bundle added


def test_ensure_toolkit_seed_tolerates_missing_bundle(isolated, monkeypatch):
    monkeypatch.setattr(sb, "bundled_toolkit_source", lambda: None)
    assert sb.ensure_toolkit_seed() == []


def test_write_session_files_seeds_toolkit(isolated, monkeypatch):
    """The seeding is wired into boot prep: every sandbox start guarantees
    the baseline exists (idempotent, copy-once)."""
    src = isolated / "bundled"
    (src / "bin").mkdir(parents=True)
    (src / "bin" / "vm-capture.ps1").write_text("# capture", encoding="utf-8")
    monkeypatch.setattr(sb, "bundled_toolkit_source", lambda: src)
    monkeypatch.setattr(sb.config_mod, "load_config", lambda: {"sandbox": {}})

    sdir = isolated / "sb" / "ws-abc"
    sb._write_session_files(sdir, Path(r"C:\proj"))

    assert (sb.toolkit_dir() / "bin" / "vm-capture.ps1").is_file()
    # second run: nothing new to write
    assert sb.ensure_toolkit_seed() == []


def test_bundled_toolkit_payload_is_valid():
    """The shipped payload itself: every repo file parses and the manifest
    matches the files actually present."""
    src = sb.bundled_toolkit_source()
    assert src is not None, "backend/bundled_toolkit missing from the repo"
    state = json.loads((src / "state.json").read_text(encoding="utf-8"))
    assert "vm-capture" in state["tools"]
    assert (src / "bin" / "vm-capture.ps1").is_file()
    assert (src / "gitconfig").is_file()
    assert "[safe]" in (src / "gitconfig").read_text(encoding="utf-8")


# ------------------------------------------------------------------ #117: persistent boot/run instrumentation


def _start_for_timing(isolated, monkeypatch):
    """Shared setup: fake spawn that writes a fully timestamped init.log."""
    monkeypatch.setattr(sb.config_mod, "load_config", lambda: {"sandbox": {}})
    monkeypatch.setattr(sb, "POLL_INTERVAL", 0.01)
    logs = isolated / "sb" / "ws-abc" / "logs"

    def fake_spawn(exe, wsb, logs_path):
        (logs_path / "init.log").write_text(
            "[2026-01-01T00:00:00.0000000] sandbox-vm-start\n"
            "[2026-01-01T00:00:01.0000000] bootstrap-step\n"
            "[2026-01-01T00:00:02.0000000] yaah-sandbox-ready",
            encoding="utf-8")
        return _FakeProc()

    monkeypatch.setattr(sb, "_spawn", fake_spawn)
    monkeypatch.setattr(sb, "session_dir",
                        lambda ws: isolated / "sb" / "ws-abc")
    return logs


def test_bootstrap_logs_per_segment_timestamps():
    script = sb._bootstrap_script()
    # Every segment gets a timestamped line, not just the final ready signal.
    assert "sandbox-vm-start" in script
    assert "bootstrap-path" in script
    assert "bootstrap-mcp" in script
    assert "yaah-sandbox-ready" in script
    # Timestamps are ISO-8601 stamped by the bootstrap itself.
    assert script.count("Get-Date -Format o") >= 4


def test_start_returns_boot_breakdown(isolated, monkeypatch):
    logs = _start_for_timing(isolated, monkeypatch)
    result = sb.start_sync(r"C:\proj")
    boot = result["boot"]
    assert boot["cold"] is True
    assert isinstance(boot["spawn_to_ready_ms"], int)
    assert boot["spawn_to_ready_ms"] >= 0
    # Breakdown keys mirror the issue's segments.
    assert set(boot["phases"]) == {"host_spawn", "guest_boot"}


def test_run_reports_elapsed_and_pickup(isolated, monkeypatch):
    logs = _start_for_timing(isolated, monkeypatch)
    sb.start_sync(r"C:\proj")
    n = sb._next_seq(logs)
    # VM-side ack carries its own clock; simulate a pickup measured by the VM.
    (logs / f"res.{n}.json").write_text(json.dumps({
        "exit_code": 0, "timed_out": False,
        "pickup_ms": 30, "run_ms": 120}), encoding="ascii")
    (logs / "output.txt").write_text("ok", encoding="utf-8-sig")
    (logs / f"done.{n}").write_text("1", encoding="ascii")
    result = sb.run_sync("Get-Date", 10)
    assert result["pickup_ms"] == 30
    assert result["run_ms"] == 120
    assert result["elapsed_ms"] >= 0


def test_run_metrics_tolerate_missing_vm_side_data(isolated, monkeypatch):
    logs = _start_for_timing(isolated, monkeypatch)
    sb.start_sync(r"C:\proj")
    n = sb._next_seq(logs)
    _write_done(logs, n, output="ok")  # old-style res file, no timings
    result = sb.run_sync("Get-Date", 10)
    assert result["elapsed_ms"] is None
    assert result["pickup_ms"] is None


def test_start_clears_stale_init_log_before_spawn(isolated, monkeypatch):
    """Issue #117: a previous boot's init.log (still containing the ready
    marker) must not satisfy the new boot's readiness poll."""
    monkeypatch.setattr(sb.config_mod, "load_config", lambda: {"sandbox": {}})
    logs = isolated / "sb" / "ws-abc" / "logs"
    logs.mkdir(parents=True)
    (logs / "init.log").write_text("yaah-sandbox-ready", encoding="utf-8")

    spawn_calls = []

    def fake_spawn(exe, wsb, logs_path):
        spawn_calls.append(1)
        assert not (logs_path / "init.log").exists()
        (logs_path / "init.log").write_text(
            "[$(date)] yaah-sandbox-ready", encoding="utf-8")
        return _FakeProc()

    monkeypatch.setattr(sb, "_spawn", fake_spawn)
    monkeypatch.setattr(sb, "session_dir",
                        lambda ws: isolated / "sb" / "ws-abc")

    sb.start_sync(r"C:\proj")
    assert spawn_calls  # the fake asserted the log was cleared pre-spawn


# --------------------------------------------------- #118: toolkit wrapper (state.json + INDEX.md)

_TOOLKIT_PS1 = Path(sb.__file__).parent.parent / "bundled_toolkit" / "bin" / "toolkit.ps1"


def _pwsh() -> str | None:
    """Best available PowerShell binary (Windows PowerShell or PowerShell
    Core), so the wrapper tests run on Linux CI with pwsh and skip cleanly
    where neither exists."""
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _run_toolkit(*args: str, tk: Path) -> subprocess.CompletedProcess:
    exe = _pwsh()
    if exe is None:
        pytest.skip("no powershell/pwsh available on this platform")
    return subprocess.run(
        [exe, "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(_TOOLKIT_PS1), *args, "-ToolkitDir", str(tk)],
        capture_output=True, text=True, timeout=60, check=False)


def test_toolkit_wrapper_install_records_state_and_index(isolated, tmp_path):
    """`toolkit install` merges the entry into state.json (installed_at
    stamped, empty fields dropped) and regenerates INDEX.md."""
    tk = tmp_path / "toolkit"
    tk.mkdir()
    (tk / "state.json").write_text(json.dumps({"tools": {
        "existing": {"version": "1.0", "kind": "zip"}}}), encoding="utf-8")

    r = _run_toolkit("install", "mytool", "-Version", "2.1",
                     "-Kind", "zip", "-Path", "mytool/bin/tool.exe",
                     "-Check", r"Test-Path '<toolkit>\mytool\bin\tool.exe'",
                     "-Invocation", r"mytool\bin\tool.exe --help",
                     tk=tk)
    assert r.returncode == 0, r.stderr

    tools = json.loads((tk / "state.json").read_text(encoding="utf-8"))["tools"]
    assert tools["existing"]["version"] == "1.0"          # untouched
    assert tools["mytool"]["version"] == "2.1"
    assert tools["mytool"]["path"] == "mytool/bin/tool.exe"
    assert tools["mytool"]["installed_at"]                # stamped
    assert "note" not in tools["mytool"]                  # empty fields dropped

    index = (tk / "INDEX.md").read_text(encoding="utf-8")
    assert "| mytool | 2.1 | zip |" in index
    assert "| existing |" in index


def test_toolkit_wrapper_install_fresh_state(isolated, tmp_path):
    """No state.json at all: the wrapper creates tools + the entry."""
    tk = tmp_path / "toolkit"
    tk.mkdir()
    r = _run_toolkit("install", "t", "-Version", "1", tk=tk)
    assert r.returncode == 0, r.stderr
    tools = json.loads((tk / "state.json").read_text(encoding="utf-8"))["tools"]
    assert tools["t"]["version"] == "1"
    assert (tk / "INDEX.md").is_file()


def test_toolkit_wrapper_remove_prints_confirmation(isolated, tmp_path):
    """A successful `toolkit remove` must SAY so on stdout — the caller
    (agent or human) has no other way to tell removal from no-op."""
    tk = tmp_path / "toolkit"
    tk.mkdir()
    assert _run_toolkit("install", "t", "-Version", "1", tk=tk).returncode == 0
    r = _run_toolkit("remove", "t", tk=tk)
    assert r.returncode == 0, r.stderr
    assert "removed" in r.stdout and "t" in r.stdout, r.stdout
    # ...and the no-op path stays distinguishable
    r2 = _run_toolkit("remove", "t", tk=tk)
    assert r2.returncode == 0
    assert "not in state.json" in r2.stdout, r2.stdout


def test_toolkit_wrapper_remove_and_idempotent_rerun(isolated, tmp_path):
    tk = tmp_path / "toolkit"
    tk.mkdir()
    assert _run_toolkit("install", "t", "-Version", "1", tk=tk).returncode == 0
    assert _run_toolkit("remove", "t", tk=tk).returncode == 0
    tools = json.loads((tk / "state.json").read_text(encoding="utf-8"))["tools"]
    assert "t" not in tools
    index = (tk / "INDEX.md").read_text(encoding="utf-8")
    assert "| t " not in index
    # removing again is a no-op, not an error
    r = _run_toolkit("remove", "t", tk=tk)
    assert r.returncode == 0


def test_toolkit_wrapper_survives_malformed_state(isolated, tmp_path):
    """A hand-mangled state.json must fail loudly, not be silently
    overwritten (the manifest is the record of every install)."""
    tk = tmp_path / "toolkit"
    tk.mkdir()
    (tk / "state.json").write_text("{not json", encoding="utf-8")
    r = _run_toolkit("install", "t", "-Version", "1", tk=tk)
    assert r.returncode != 0
    assert (tk / "state.json").read_text(encoding="utf-8") == "{not json"


def test_bundled_toolkit_ships_wrapper_and_index():
    """The shipped payload includes the wrapper and a generated INDEX.md
    consistent with the bundled state.json."""
    src = sb.bundled_toolkit_source()
    assert (src / "bin" / "toolkit.ps1").is_file()
    assert (src / "bin" / "toolkit.cmd").is_file()
    index = (src / "INDEX.md").read_text(encoding="utf-8")
    tools = json.loads((src / "state.json").read_text(encoding="utf-8"))["tools"]
    for name in tools:
        assert f"| {name} " in index, name


def test_ensure_toolkit_seed_copies_wrapper_and_index(isolated, monkeypatch, tmp_path):
    """The wrapper + INDEX.md are part of the seeded baseline (copy-once,
    like every other bundled file)."""
    src = tmp_path / "bundled"
    (src / "bin").mkdir(parents=True)
    (src / "bin" / "toolkit.ps1").write_text("# wrapper", encoding="utf-8")
    (src / "bin" / "toolkit.cmd").write_text("@echo off", encoding="utf-8")
    (src / "INDEX.md").write_text("# Toolkit index", encoding="utf-8")
    (src / "state.json").write_text(json.dumps({"tools": {}}), encoding="utf-8")
    monkeypatch.setattr(sb, "bundled_toolkit_source", lambda: src)

    sb.ensure_toolkit_seed()

    tk = sb.toolkit_dir()
    assert (tk / "bin" / "toolkit.ps1").is_file()
    assert (tk / "INDEX.md").is_file()


# --- CodeRabbit findings on the closed PR #159 (issue #118 re-land) ---------

def test_toolkit_wrapper_install_is_serialized(isolated, tmp_path):
    """Finding 1 (major): concurrent installs must not lose an entry.
    The wrapper takes a cross-process lock through the whole
    read-mutate-write and uses a per-process temp file."""
    tk = tmp_path / "toolkit"
    tk.mkdir()
    procs = []
    exe = _pwsh()
    if exe is None:
        pytest.skip("no powershell/pwsh available on this platform")
    for i in range(6):
        procs.append(subprocess.Popen(
            [exe, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(_TOOLKIT_PS1), "install", f"tool{i}",
             "-Version", str(i), "-ToolkitDir", str(tk)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
    errs = [p.communicate()[1].decode(errors="replace") for p in procs]
    rcs = [p.returncode for p in procs]
    assert all(rc == 0 for rc in rcs), list(zip(rcs, errs))
    tools = json.loads((tk / "state.json").read_text(encoding="utf-8"))["tools"]
    assert sorted(t for t in tools if t.startswith("tool")) == \
        [f"tool{i}" for i in range(6)]
    assert not list(tk.glob("state.json.tmp*")), "temp file must be unique/cleaned"


def test_toolkit_wrapper_reinstall_preserves_metadata(isolated, tmp_path):
    """Finding 2: reinstalling with fewer fields must not wipe existing
    metadata; supplied values override, others survive."""
    tk = tmp_path / "toolkit"
    tk.mkdir()
    assert _run_toolkit(
        "install", "t", "-Version", "1", "-Kind", "zip",
        "-Path", "t/tool.exe", "-Note", "gotcha", tk=tk).returncode == 0
    assert _run_toolkit("install", "t", "-Version", "2", tk=tk).returncode == 0
    tools = json.loads((tk / "state.json").read_text(encoding="utf-8"))["tools"]
    entry = tools["t"]
    assert entry["version"] == "2"          # supplied value overrides
    assert entry["kind"] == "zip"           # pre-existing fields survive
    assert entry["path"] == "t/tool.exe"
    assert entry["note"] == "gotcha"


def test_toolkit_wrapper_index_multiline_note_stays_one_line(isolated, tmp_path):
    """A multiline `note` must not break the INDEX.md table (CodeRabbit
    return trip on #248): cells normalize CR/LF to spaces."""
    tk = tmp_path / "toolkit"
    tk.mkdir()
    r = _run_toolkit("install", "t", "-Version", "1",
                     "-Note", "line one\nline two", tk=tk)
    assert r.returncode == 0, r.stderr
    index = (tk / "INDEX.md").read_text(encoding="utf-8")
    row = next(ln for ln in index.splitlines() if ln.startswith("| t |"))
    assert "line one line two" in row        # flattened
    assert "\n" not in row and "\r" not in row


def test_toolkit_wrapper_index_escapes_cells(isolated, tmp_path):
    """Finding 3: pipe/newline in metadata renders escaped in INDEX.md
    (table not broken); state.json keeps the value verbatim."""
    tk = tmp_path / "toolkit"
    tk.mkdir()
    r = _run_toolkit("install", "t", "-Version", "1",
                     "-Check", r"Get-Command x | Select-Object Source",
                     tk=tk)
    assert r.returncode == 0, r.stderr
    state = (tk / "state.json").read_text(encoding="utf-8")
    assert "Get-Command x | Select-Object Source" in state   # verbatim
    index = (tk / "INDEX.md").read_text(encoding="utf-8")
    row = next(ln for ln in index.splitlines() if ln.startswith("| t |"))
    assert "Get-Command x \\| Select-Object Source" in row


def test_sandbox_schema_wrapper_path_has_no_escape_sequences():
    """Finding 4: the tool description must not contain \\b or \\t control
    characters from unescaped backslashes in the path."""
    text = "".join(
        (p.get("function", {}).get("description") or "")
        for p in sb.SANDBOX_TOOLS_SCHEMA)
    assert "\b" not in text and "\t" not in text
    assert "toolkit\\bin\\toolkit.ps1" in text


def test_ensure_toolkit_seed_regenerates_stale_index(isolated, monkeypatch,
                                                     tmp_path):
    """Finding 5: after a bundled-entries merge into an existing toolkit,
    a stale INDEX.md is regenerated from the merged manifest."""
    src = tmp_path / "bundled"
    src.mkdir()
    (src / "state.json").write_text(json.dumps({"tools": {
        "newbundled": {"version": "1", "kind": "zip"}}}), encoding="utf-8")
    (src / "INDEX.md").write_text("# Toolkit index", encoding="utf-8")
    monkeypatch.setattr(sb, "bundled_toolkit_source", lambda: src)

    tk = sb.toolkit_dir()
    tk.mkdir(parents=True, exist_ok=True)
    (tk / "INDEX.md").write_text("# stale index without the new entry",
                                 encoding="utf-8")

    sb.ensure_toolkit_seed()

    index = (tk / "INDEX.md").read_text(encoding="utf-8")
    assert "newbundled" in index, "INDEX.md regenerated after merge"


def test_ensure_toolkit_seed_index_flattens_multiline_cells(isolated,
                                                            monkeypatch,
                                                            tmp_path):
    """CodeRabbit return trip on #248: a multiline note in an existing
    toolkit entry must render on one line in the regenerated INDEX.md."""
    src = tmp_path / "bundled"
    src.mkdir()
    (src / "state.json").write_text(json.dumps({"tools": {
        "newbundled": {"version": "1", "kind": "zip"}}}), encoding="utf-8")
    monkeypatch.setattr(sb, "bundled_toolkit_source", lambda: src)

    tk = sb.toolkit_dir()
    tk.mkdir(parents=True, exist_ok=True)
    (tk / "state.json").write_text(json.dumps({"tools": {
        "multi": {"version": "2", "kind": "zip",
                  "note": "line one\nline two"}}}), encoding="utf-8")

    sb.ensure_toolkit_seed()

    index = (tk / "INDEX.md").read_text(encoding="utf-8")
    row = next(ln for ln in index.splitlines() if ln.startswith("| multi |"))
    assert "line one line two" in row


def test_ensure_toolkit_seed_holds_manifest_lock(isolated, monkeypatch, tmp_path):
    """CodeRabbit return trip on #248: a live VM's `toolkit install` holds
    Global\\yaah-toolkit-manifest across its read-mutate-write; if the seed
    doesn't take the same lock, its older merged snapshot can clobber the
    freshly installed entry. A helper process grabs the lock first and
    writes its entry late (while holding it): the seeded bundled entry and
    the helper's entry must BOTH survive. Without the seed-side lock the
    seed's early write is overwritten and the bundled entry is lost."""
    import ctypes
    import sys
    if not hasattr(ctypes, "windll"):
        pytest.skip("named-mutex test requires Windows")

    src = tmp_path / "bundled"
    src.mkdir()
    (src / "state.json").write_text(json.dumps({"tools": {
        "newbundled": {"version": "1", "kind": "zip"}}}), encoding="utf-8")
    monkeypatch.setattr(sb, "bundled_toolkit_source", lambda: src)

    tk = sb.toolkit_dir()
    tk.mkdir(parents=True, exist_ok=True)
    (tk / "state.json").write_text(json.dumps({"tools": {}}), encoding="utf-8")

    helper = (
        "import ctypes, json, time\n"
        "h = ctypes.windll.kernel32.CreateMutexW(None, False,\n"
        "                                        r'Global\\yaah-toolkit-manifest')\n"
        "assert ctypes.windll.kernel32.WaitForSingleObject(h, 30000) == 0\n"
        f"p = {str(tk / 'state.json')!r}\n"
        "time.sleep(1.5)\n"          # hold the lock past the seeder's start
        "json.dump({'tools': {'marker': {'version': '9'}}}, open(p, 'w'))\n"
        "ctypes.windll.kernel32.ReleaseMutex(h)\n")
    proc = subprocess.Popen([sys.executable, "-c", helper])
    try:
        time.sleep(0.4)              # the helper now holds the lock
        sb.ensure_toolkit_seed()
        time.sleep(3)                # let the helper finish its write
        tools = json.loads(
            (tk / "state.json").read_text(encoding="utf-8")).get("tools") or {}
        assert "newbundled" in tools, (
            "seed's bundled entry lost — ensure_toolkit_seed() must hold "
            "Global\\yaah-toolkit-manifest across its read-merge-write")
        assert "marker" in tools
    finally:
        proc.wait(timeout=30)


def test_readme_atomicity_wording_is_accurate(isolated):
    """Finding 6: the README must not claim INDEX.md updates are atomic —
    state.json is temp+move, INDEX.md is regenerated after it."""
    readme = (sb.bundled_toolkit_source() / "README.md").read_text(
        encoding="utf-8")
    assert "atomically" not in readme.lower()
    assert "regenerates" in readme
    # copy-once: the FIRST seed writes the baseline into the isolated
    # toolkit, the SECOND must be a no-op (running against the host's real
    # toolkit would see the newly bundled files as "new" on the first CI
    # run after an upgrade).
    assert sb.ensure_toolkit_seed()  # baseline seeded into the temp toolkit
    assert sb.ensure_toolkit_seed() == []
