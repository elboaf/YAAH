"""Windows Sandbox integration tests.

No test requires a real sandbox or the Windows feature: generation, config,
risk classes and the run protocol are exercised through fakes (YAAH_SANDBOX_EXE
override + fake spawn), so the suite is green on Linux CI too.
"""
import asyncio
import json
import re
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


def test_bootstrap_signals_ready_and_batches(isolated, monkeypatch):
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    script = sb._bootstrap_script()
    assert "yaah-sandbox-ready" in script
    assert sb.SB_TOOLKIT in script  # toolkit on PATH
    # the processed-set guard against re-running old command files
    assert "$processed" in script


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
    monkeypatch.setattr(sb, "_sandbox_pids", lambda: [])

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
    # ack any command file dropped into the logs dir (the nonce handshake)
    monkeypatch.setattr(sb.Path, "write_text",
                        _ack_on_cmd_write(logs, ack_nonce))

    result = sb.start_sync("C:\\proj")
    assert result["status"] == "running"
    assert result["note"].startswith("re-attached")
    assert spawned == []  # no second VM


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
        return None

    return fake


def test_prompt_section_mentions_toolkit_and_gates(isolated):
    text = sb.prompt_section()
    assert "sandbox_test" in text and "sandbox_run" in text
    assert "toolkit" in text
    assert "ask mode" in text


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
    monkeypatch.setattr(sb.config_mod, "load_config",
                        lambda: {"sandbox": {}})
    from backend.agent.tools import execute_tool

    result = asyncio.run(
        execute_tool("sandbox_status", {}, "C:\\proj"))
    assert result["available"] is True
    assert result["running"] is False


def test_execute_tool_rejects_bad_sandbox_run_args(isolated):
    from backend.agent.tools import execute_tool

    result = asyncio.run(
        execute_tool("sandbox_run", {"command": "   "}, "C:\\proj"))
    assert "error" in result
    result = asyncio.run(
        execute_tool("sandbox_run", {"command": "dir", "timeout_seconds": "x"},
                     "C:\\proj"))
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
    assert "# Windows Sandbox (live verification)" in prompt
    assert "persistent dev toolkit" in prompt
