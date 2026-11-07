"""GUI front-ends for the Windows server (scripts/server_gui.py).

The pure decision helpers (config pre-fill, field validation, the
uninstall plan) are tested here headlessly; the tkinter windows
themselves are exercised manually (the frozen exe's --windowed build),
same split as the service tests: decision logic here, live behavior on
a real box.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

from backend.agent.config import load_config, save_config


def _load_gui():
    """Import scripts/server_gui.py as a module (scripts/ is not a package)."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "server_gui.py"
    spec = importlib.util.spec_from_file_location("server_gui", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gui():
    return _load_gui()


# ---------------------------------------------------------------- config pre-fill

def test_load_existing_remote_reads_config(gui, tmp_path, monkeypatch):
    """The wizard pre-fills from the profile's .yaah/config.json."""
    cfg_dir = tmp_path / ".yaah"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        '{"remote": {"passphrase": "hunter2", "display_name": "lab-box"}}',
        encoding="utf-8")
    remote = gui.load_existing_remote(str(tmp_path))
    assert remote.get("passphrase") == "hunter2"
    assert remote.get("display_name") == "lab-box"


def test_load_existing_remote_missing_file_is_empty(gui, tmp_path):
    """A fresh box has no config: the wizard shows raw defaults."""
    assert gui.load_existing_remote(str(tmp_path)) == {}


def test_load_existing_remote_none_profile_is_empty(gui):
    assert gui.load_existing_remote(None) == {}


def test_load_existing_remote_corrupt_config_is_empty(gui, tmp_path):
    """A corrupt config must not crash the wizard launch - just defaults."""
    cfg_dir = tmp_path / ".yaah"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("{not json", encoding="utf-8")
    assert gui.load_existing_remote(str(tmp_path)) == {}


def test_load_existing_remote_non_dict_remote_is_empty(gui, tmp_path):
    """remote: [] (or any non-dict) is not a usable pre-fill source."""
    cfg_dir = tmp_path / ".yaah"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text('{"remote": [1, 2]}', encoding="utf-8")
    assert gui.load_existing_remote(str(tmp_path)) == {}


# ---------------------------------------------------------------- validation

def test_validate_happy_path_defaults(gui):
    """Empty display falls back to the hostname at the caller; the
    validator itself only requires the passphrase and a sane port."""
    errors, vals = gui.validate_install_fields("s3cret", "", "", "")
    assert errors == []
    assert vals == {"passphrase": "s3cret", "display": "",
                    "host": "0.0.0.0", "port": 8765}


def test_validate_requires_passphrase(gui):
    """The empty-passphrase rule (a server without one refuses all remote
    access) must block the install, not warn."""
    errors, _vals = gui.validate_install_fields("", "name", "8765", "")
    assert any("passphrase" in e for e in errors)


def test_validate_rejects_non_numeric_port(gui):
    errors, _vals = gui.validate_install_fields("pw", "name", "eighty", "")
    assert any("port must be a number" in e for e in errors)


def test_validate_rejects_out_of_range_port(gui):
    for bad in ("0", "65536", "99999"):
        errors, _vals = gui.validate_install_fields("pw", "name", bad, "")
        assert any("1 and 65535" in e for e in errors), bad


def test_validate_strips_whitespace(gui):
    """Typed-with-trailing-space values must not store ' pw ' or ' 127.0.0.1 '."""
    _errors, vals = gui.validate_install_fields("  pw  ", " name ", "9000", " 127.0.0.1 ")
    assert vals["passphrase"] == "pw"
    assert vals["display"] == "name"
    assert vals["host"] == "127.0.0.1"
    assert vals["port"] == 9000


# ---------------------------------------------------------------- uninstall plan

def test_plan_defaults_keep_data(gui):
    """Default checkboxes: service + program go, the shared data folder
    stays - and the summary must say so explicitly."""
    lines = gui.plan_uninstall(True, False, True,
                               profile_dir=r"C:\Users\u", installed="x.exe")
    assert any("YaahServer service" in ln for ln in lines)
    assert any("Programs & Features" in ln for ln in lines)
    assert not any("delete" in ln and ".yaah" in ln for ln in lines)
    assert any("kept" in ln and "data folder" in ln for ln in lines)


def test_plan_all_options(gui):
    lines = gui.plan_uninstall(True, True, True,
                               profile_dir=r"C:\Users\u", installed="x.exe")
    assert any("service" in ln for ln in lines)
    assert any(".yaah" in ln and "delete" in ln.lower() for ln in lines)
    assert any("x.exe" in ln for ln in lines)
    assert not any("kept" in ln for ln in lines)


def test_plan_service_opt_out(gui):
    """Unchecking the service must promise to keep it - not silently
    remove it anyway."""
    lines = gui.plan_uninstall(False, False, False,
                               profile_dir=r"C:\Users\u", installed="x.exe")
    assert any("keep the service" in ln for ln in lines)


# ---------------------------------------------------------------- GUI windows (subprocess-isolated)

# The three window-drive tests spawn gui_drive_worker.py as a fresh
# interpreter: sequential threaded Tk() interpreters in one process
# accumulate per-process Tcl state and crash at gc (Tcl_AsyncDelete /
# fatal 0x80000003) - Tkinter must own its process. The worker drives
# the real window (scripted clicks, faked dialogs) and reports one JSON
# line; this test process never imports tkinter.

WORKER = Path(__file__).parent / "gui_drive_worker.py"


def _drive(scenario: str, tmp_path: Path) -> dict:
    """Run one worker scenario; return its JSON report. Fails on a
    non-zero exit (the worker's 8s watchdog exits 2 on a hang)."""
    import json
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(WORKER), str(tmp_path), scenario],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        assert False, (f"worker {scenario} exited {proc.returncode}: "
                       f"{proc.stderr[-600:]}")
    lines = [l for l in proc.stdout.splitlines() if l.startswith("@@RESULT@@")]
    assert lines, f"no result line: {proc.stdout[-400:]}"
    return json.loads(lines[0][len("@@RESULT@@"):])


def test_wizard_installs_with_prefilled_values(tmp_path):
    """Full GUI loop in a fresh interpreter: config pre-fill -> scripted
    Install click -> worker -> confirmation dialog with the real port.
    The values reaching _do_install are the pre-filled ones - upgrades
    must not retype anything, and progress goes through the queue."""
    report = _drive("install", tmp_path)
    assert report["rc"] == 0
    if not report.get("headless"):
        # real-window run: the worker surfaced progress through the queue
        assert report["emit_used"], "install progress must flow via emit()"
    assert len(report["install_calls"]) == 1
    call = report["install_calls"][0]
    assert call["passphrase"] == "hunter2"
    assert call["display"] == "box"
    assert call["port"] == 9000
    assert call["host"] == "0.0.0.0"
    assert call["profile"] == str(tmp_path)
    assert any("9000" in d["message"] for d in report["dialogs"]
               if d["kind"] == "info"), report["dialogs"]


def test_wizard_blocks_empty_passphrase_with_error_dialog(tmp_path):
    """No pre-fill (empty config) + cleared passphrase -> the scripted
    click must be blocked with a visible error, never a half-install."""
    report = _drive("install-noseed", tmp_path)
    assert report["rc"] == 0
    assert report["install_calls"] == [], "must not half-install"
    assert any(d["kind"] == "error" and "passphrase" in d["message"]
               for d in report["dialogs"]), report["dialogs"]


def test_uninstaller_defaults_remove_service_and_exe_keep_data(tmp_path):
    """Defaults, driven end-to-end: service + program go, the shared
    data folder stays, and the completion report says exactly that."""
    report = _drive("uninstall", tmp_path)
    assert report["rc"] == 0
    installed = tmp_path / "yaah-server-setup.exe"
    assert not installed.exists(), "exe checkbox on: the file must go"
    assert (tmp_path / ".yaah").is_dir(), "default keeps the data folder"
    done = [d for d in report["dialogs"] if d["kind"] == "info"]
    assert done, report["dialogs"]
    msg = done[-1]["message"]
    assert "Removed" in msg
    assert "YaahServer service" in msg
    assert str(installed) in msg


def test_uninstaller_config_opt_in_deletes_data_folder(tmp_path):
    """Checking the data-folder box must really delete it (and say so in
    the report) - the opt-in path the summary promises."""
    report = _drive("uninstall-data", tmp_path)
    assert report["rc"] == 0
    assert not (tmp_path / ".yaah").exists(), "opted-in data folder must go"
    done = [d for d in report["dialogs"] if d["kind"] == "info"]
    assert done and ".yaah" in done[-1]["message"]
