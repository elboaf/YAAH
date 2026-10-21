"""Headless server entry point (scripts/server_entry.py).

The server binary is the desktop backend's host role without a GUI:
--passphrase/--display-name persist to the shared config store (the same
file the desktop app's Settings writes), and per-run env markers steer
the lifespan (no computer-use hooks, optional mDNS beacon). The remote
protocol itself is covered by test_remote.py — none of it changes here.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

from backend.agent.config import load_config, save_config

_uvicorn_calls: dict = {}


def _load_entry():
    """Import scripts/server_entry.py as a module (scripts/ is not a package)."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "server_entry.py"
    spec = importlib.util.spec_from_file_location("server_entry", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _fake_uvicorn(monkeypatch):
    """main() must never actually serve in tests; record the bind instead."""
    import uvicorn

    _uvicorn_calls.clear()

    def fake_run(app, host, port, log_level="info"):  # noqa: ARG001
        _uvicorn_calls["bind"] = (host, port)

    monkeypatch.setattr(uvicorn, "run", fake_run)


@pytest.fixture
def entry():
    return _load_entry()


# ---------------------------------------------------------------- config persistence

def test_passphrase_persists_to_shared_config(entry):
    """Passphrase written via CLI persists to shared config.json."""
    rc = entry.main(["--passphrase", "hunter2"])
    assert rc == 0
    assert (load_config().get("remote") or {}).get("passphrase") == "hunter2"


def test_display_name_persists_to_shared_config(entry):
    """Display name written via CLI persists to shared config.json."""
    entry.main(["--display-name", "lab-box"])
    assert (load_config().get("remote") or {}).get("display_name") == "lab-box"


def test_passphrase_save_preserves_other_remote_keys(entry):
    """The merge must not clobber the rest of the remote block (e.g. the
    display name the desktop app shows)."""
    save_config({"remote": {"display_name": "keepme", "hosting_enabled": False}})
    entry.main(["--passphrase", "s3cret"])
    remote = load_config().get("remote") or {}
    assert remote.get("passphrase") == "s3cret"
    assert remote.get("display_name") == "keepme"
    assert remote.get("hosting_enabled") is False


def test_no_flags_touch_nothing(entry):
    """A bare start (the service case: passphrase already in config) must
    not rewrite the config file at all."""
    before = load_config()
    entry.main([])
    assert load_config() == before


# ---------------------------------------------------------------- run markers

def test_headless_marker_is_always_set(entry, monkeypatch):
    monkeypatch.delenv("YAAH_HEADLESS", raising=False)
    entry.main([])
    assert os.environ["YAAH_HEADLESS"] == "1"


def test_no_hosting_flag_sets_marker_only(entry, monkeypatch):
    """--no-hosting is per-run (env marker), not persisted — the stored
    hosting_enabled stays untouched."""
    monkeypatch.delenv("YAAH_NO_HOSTING", raising=False)
    save_config({"remote": {"hosting_enabled": True}})
    entry.main(["--no-hosting"])
    assert os.environ["YAAH_NO_HOSTING"] == "1"
    assert (load_config().get("remote") or {}).get("hosting_enabled") is True


# ---------------------------------------------------------------- bind args

def test_default_bind_is_lan_hosting(entry):
    entry.main([])
    assert _uvicorn_calls["bind"] == ("0.0.0.0", 8765)


def test_bind_overrides(entry):
    entry.main(["--host", "127.0.0.1", "--port", "9999"])
    assert _uvicorn_calls["bind"] == ("127.0.0.1", 9999)


# ---------------------------------------------------------------- service dispatch

def test_service_verbs_refused_off_windows_or_without_pywin32(entry, monkeypatch):
    """On Linux (or a Windows build lacking pywin32) the verbs must fail
    loudly rather than fall through to CLI parsing (which would argparse-
    error on the unknown positional)."""
    monkeypatch.setattr(entry, "_service_class", lambda: None)
    assert entry._handle_service_command(["install"]) == 1


def test_non_service_argv_is_not_swallowed(entry):
    """Normal CLI runs (flags only) must pass through to main()."""
    assert entry._handle_service_command(["--passphrase", "x"]) is None


def test_entry_routes_service_verb_without_serving(entry, monkeypatch):
    """entry() with a service verb on a pywin32-less box exits before any
    uvicorn bind; bare flags still reach main()."""
    monkeypatch.setattr(entry, "_handle_service_command", lambda a: 1)
    assert entry.entry(["install"]) == 1
    assert "bind" not in _uvicorn_calls


def test_install_flags_before_verb_reach_handle_commandline(entry, monkeypatch):
    """The setup wizard invokes `exe --startup auto install` (LocalSystem
    default: no --username/--password). pywin32's HandleCommandLine
    parses with getopt, which stops at the first positional: the verb
    must be the ONLY/last positional or it prints usage and exits 1
    (broke v0.16.4 AND v0.16.5 — verified live against pywin32).
    Routing itself must accept flags before the verb."""
    import types

    captured = {}
    fake_mod = types.ModuleType("win32serviceutil")
    fake_mod.HandleCommandLine = lambda cls, argv: captured.update(argv=argv)
    monkeypatch.setattr(entry, "_service_class", lambda: object())
    monkeypatch.setitem(sys.modules, "win32serviceutil", fake_mod)
    argv = ["--startup", "auto", "install"]
    assert entry._handle_service_command(argv) == 0
    assert captured["argv"] == [""] + argv


# ---------------------------------------------------------------- service start truthfulness

def test_start_and_await_polls_scm_not_exit_code(entry, monkeypatch):
    """HandleCommandLine always exits 0 — the wizard must read the actual
    service state instead (v0.16.x printed "installed and running" over a
    service that never started: 1069 logon failure)."""
    import subprocess as subprocess_mod
    import types

    started = []

    def fake_run(cmd, *a, **k):
        started.append([str(c) for c in cmd])
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess_mod, "run", fake_run)
    monkeypatch.setattr(entry, "_service_state", lambda: "4  RUNNING")
    state = entry._start_and_await("some.exe", timeout=2)
    assert state.endswith("RUNNING")
    assert started == [["some.exe", "start"]]


def test_start_and_await_gives_up_on_stopped(entry, monkeypatch):
    """A STOPPED state must end the wait immediately (no point polling
    the full timeout when the SCM already gave a final answer)."""
    import types

    import subprocess as subprocess_mod
    monkeypatch.setattr(
        subprocess_mod, "run",
        lambda cmd, *a, **k: types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(entry, "_service_state", lambda: "1  STOPPED")
    assert entry._start_and_await("some.exe", timeout=10) == "1  STOPPED"


def _fake_winreg(monkeypatch, profile=None, fail_open=False, fail_write=False):
    """A fake winreg so wizard/service tests never touch the real HKLM.

    Records ProfileDir writes; fail_open/fail_write simulate registry
    failures for the warning paths."""
    import types

    written: dict = {}
    fake = types.ModuleType("winreg")
    fake.KEY_READ, fake.KEY_WRITE, fake.KEY_WOW64_64KEY = 1, 2, 512
    fake.REG_SZ = 1
    fake.HKEY_LOCAL_MACHINE = 0x80000002

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def open_key_ex(*a, **k):
        if fail_open:
            raise OSError("registry key missing")
        return _Key()

    def query_value_ex(key, name):
        return profile, fake.REG_SZ

    def create_key_ex(*a, **k):
        if fail_write:
            raise OSError("access denied")
        return _Key()

    def set_value_ex(key, name, _reserved, _typ, val):
        written[name] = val

    fake.OpenKeyEx = open_key_ex
    fake.QueryValueEx = query_value_ex
    fake.CreateKeyEx = create_key_ex
    fake.SetValueEx = set_value_ex
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return written


def _windows_only():
    """Skip on non-Windows: the wizard and the service-context env apply
    only exist there (winreg, HKLM, HOMEDRIVE). Same convention as the
    pre-#459 wizard test's platform guard."""
    if sys.platform != "win32":
        pytest.skip("Windows-only code path (wizard/service + winreg)")


def _install_wizard(entry, monkeypatch, capsys, state="4  RUNNING",
                    **winreg_kwargs):
    """Drive _run_setup_wizard end-to-end with every external touch faked.

    Returns (exit code, captured output, list of subprocess argv lists,
    ProfileDir writes from the fake winreg)."""
    _windows_only()
    import getpass as getpass_mod
    import subprocess as subprocess_mod
    import types

    monkeypatch.setattr(entry, "_is_elevated", lambda: True)
    monkeypatch.setattr(entry, "_persist_remote", lambda updates: None)
    monkeypatch.setattr(entry, "_service_state", lambda: state)
    answers = iter(["secret-pass"])
    monkeypatch.setattr(getpass_mod, "getpass", lambda *a, **k: next(answers))
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append([str(c) for c in cmd])
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess_mod, "run", fake_run)
    # Self-copy skip: point _installed_exe at THIS process's own exe so the
    # wizard sees source == dest and never touches the disk.
    monkeypatch.setattr(entry, "_installed_exe", lambda: sys.executable)
    written = _fake_winreg(monkeypatch, **winreg_kwargs)
    rc = entry._run_setup_wizard()
    return rc, capsys.readouterr().out, calls, written


def test_wizard_installs_local_system_without_password(entry, monkeypatch,
                                                       capsys):
    """#459: the wizard must never ask for a Windows account password and
    must register the service with NO --username/--password — CreateService
    then defaults the account to LocalSystem."""
    rc, out, calls, written = _install_wizard(entry, monkeypatch, capsys)
    assert rc == 0
    assert "RUNNING" in entry._service_state()
    install = [c for c in calls if c[-1] == "install"]
    assert install == [[sys.executable, "--startup", "auto", "install"]]
    assert not any("--username" in c or "--password" in c for c in calls)
    assert not hasattr(entry, "_grant_logon_right")  # deleted with its purpose
    assert "Windows password" not in out
    # The profile the passphrase was written to is recorded for the service.
    import os as os_mod

    assert written["ProfileDir"] == os_mod.path.expanduser("~")
    assert "LocalSystem" in out


def test_wizard_warns_but_starts_when_profile_dir_fails(entry, monkeypatch,
                                                        capsys):
    """A failed Parameters write must not abort the install — the service
    still comes up (config-less: it refuses remote requests until setup
    is re-run), with a warning that says exactly that."""
    rc, out, calls, _w = _install_wizard(
        entry, monkeypatch, capsys, fail_write=True)
    assert rc == 0
    assert "could not record the profile dir" in out
    assert [c for c in calls if c[-1] == "install"]


def test_wizard_fails_when_service_never_reaches_running(entry, monkeypatch,
                                                         capsys):
    """The whole point of the v0.16.x bug: a failed start must exit 1 and
    say so — never print "installed and running" over a dead service."""
    rc, out, _calls, _w = _install_wizard(
        entry, monkeypatch, capsys, state="1  STOPPED")
    assert rc == 1
    assert "did not reach RUNNING" in out
    assert "installed and running" not in out


# ---------------------------------------------------------------- profile-dir application

def test_apply_service_profile_dir_sets_user_env(entry, monkeypatch,
                                                 tmp_path):
    """SvcDoRun re-points the per-user paths at the recorded profile
    BEFORE backend imports resolve Path.home() — USERPROFILE, HOME and
    HOMEDRIVE+HOMEPATH must all land on that directory."""
    _windows_only()
    profile = str(tmp_path)
    _fake_winreg(monkeypatch, profile=profile)
    for var in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME"):
        monkeypatch.setenv(var, "placeholder")  # records original for undo
        monkeypatch.setenv(var, "placeholder")  # records original for undo
        monkeypatch.delenv(var)
    assert entry._apply_service_profile_dir() is True
    import os as os_mod

    assert os_mod.environ["USERPROFILE"] == profile
    assert os_mod.environ["HOME"] == profile
    drive, rest = os_mod.path.splitdrive(profile)
    assert os_mod.environ["HOMEDRIVE"] == drive
    assert os_mod.environ["HOMEPATH"] == rest


def test_apply_service_profile_dir_missing_value_is_noop(entry, monkeypatch):
    """No recorded profile (or unreadable key): leave SYSTEM defaults
    alone — the config-less state, not a crash."""
    _windows_only()
    _fake_winreg(monkeypatch, fail_open=True)
    before = dict(os.environ)
    assert entry._apply_service_profile_dir() is False
    assert {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")} == \
        {k: before.get(k) for k in ("USERPROFILE", "HOME")}


def test_apply_service_profile_dir_rejects_bogus_dir(entry, monkeypatch):
    """A stale ProfileDir (profile deleted since install) must be ignored,
    not handed to Path.home()."""
    _windows_only()
    _fake_winreg(monkeypatch, profile=r"C:\no\such\profile")
    assert entry._apply_service_profile_dir() is False
