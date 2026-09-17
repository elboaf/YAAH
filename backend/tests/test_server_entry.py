"""Headless server entry point (scripts/server_entry.py).

The server binary is the desktop backend's host role without a GUI:
--passphrase/--display-name persist to the shared config store (the same
file the desktop app's Settings writes), and per-run env markers steer
the lifespan (no computer-use hooks, optional mDNS beacon). The remote
protocol itself is covered by test_remote.py — none of it changes here.
"""
import importlib.util
import os
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
    rc = entry.main(["--passphrase", "hunter2"])
    assert rc == 0
    assert (load_config().get("remote") or {}).get("passphrase") == "hunter2"


def test_display_name_persists_to_shared_config(entry):
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
