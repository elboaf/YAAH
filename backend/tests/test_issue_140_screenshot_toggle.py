"""Issue #140: a Settings toggle to allow/disallow the `screenshot` tool.

Default is ALLOWED (behavior unchanged). When disallowed:

- ``get_schemas`` never returns the screenshot schema;
- the system prompt does not advertise it;
- a stray call returns a graceful pointer to the setting, never a traceback;
- the choice persists via the global config (``computer_use.allow_screenshot``).
"""

import os

import pytest

from backend.agent import config, tools


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    # Redirect config to a per-test file. CONFIG_PATH is captured from the
    # environment at import time, so the module needs a reload to see the
    # override — and every attribute the reload rebinds must be put back
    # afterwards, or the rest of the suite keeps saving into this throwaway
    # tmp file and later tests (e.g. test_remote's device-profile save)
    # read back a config missing the keys they expect (the #140 CI run's
    # remote_devices KeyError). A second importlib.reload at teardown would
    # NOT work: monkeypatch has already removed the env var, so the reload
    # would rebind CONFIG_PATH to the default backend/data path instead of
    # the suite-wide path conftest installed. Snapshot and setattr instead.
    monkeypatch.setenv("YAAH_CONFIG_PATH", str(tmp_path / "config.json"))
    import importlib

    saved = dict(vars(config))
    importlib.reload(config)
    yield config
    monkeypatch.delenv("YAAH_CONFIG_PATH")
    for key, value in saved.items():
        setattr(config, key, value)


def _disallow(c):
    c.save_config({"computer_use": {"allow_screenshot": False}})


def test_allowed_by_default(cfg):
    assert cfg.load_config()["computer_use"]["allow_screenshot"] is True


def test_schemas_include_screenshot_by_default(cfg):
    names = {s["function"]["name"] for s in tools.get_schemas()}
    if "screenshot" in tools.EXECUTORS:  # windows-only toolset
        assert "screenshot" in names


def test_schemas_exclude_screenshot_when_disallowed(cfg):
    _disallow(cfg)
    names = {s["function"]["name"] for s in tools.get_schemas()}
    assert "screenshot" not in names


def test_stray_call_returns_graceful_pointer(cfg):
    if "screenshot" not in tools.EXECUTORS:
        # Non-Windows toolset: the computer-use executors are never
        # registered (backend/agent/tools.py imports computer.py behind
        # os.name == "nt"), so there is no stray call to intercept here —
        # get_schemas' filtering (above) is the whole platform contract.
        pytest.skip("screenshot executor only exists on Windows")
    _disallow(cfg)
    import asyncio

    result = asyncio.run(
        tools.execute_tool("screenshot", {"monitor": 1}, workspace=".")
    )
    text = str(result)
    assert "error" not in result or "Settings" in text
    assert "screenshot" in text
    assert "Traceback" not in text
    assert "Settings" in text


def test_setting_persists(cfg):
    _disallow(cfg)
    assert cfg.load_config()["computer_use"]["allow_screenshot"] is False


def test_settings_api_roundtrip(tmp_path, monkeypatch):
    """PUT /api/config merges the computer_use block (#140): a save that
    only touches allow_screenshot must not wipe sibling keys."""
    from fastapi.testclient import TestClient

    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    import backend.main as mainmod

    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    monkeypatch.setattr(mainmod, "save_config", cfgmod.save_config)
    from backend.main import app

    with TestClient(app) as client:
        r = client.put(
            "/api/config", json={"computer_use": {"allow_screenshot": False}}
        )
        assert r.status_code == 200
        cfg = cfgmod.load_config()
        assert cfg["computer_use"]["allow_screenshot"] is False
        # Sibling keys of the stored block survive a partial save.
        assert cfg["computer_use"]["panic_hotkey"] == "<ctrl>+<alt>+y"
        got = client.get("/api/config").json()
        assert got["computer_use"]["allow_screenshot"] is False


def test_system_prompt_suppresses_screenshot_when_disallowed(cfg):
    if os.name != "nt":
        # Same skip as the allowed-case sibling below: on POSIX the prompt
        # never contains the computer-use section at all (the `if windows`
        # branch is skipped), so there is no suppression to assert — and
        # faking os.name = "nt" is NOT an alternative: pathlib.Path()
        # consults os.name per call, so a fake would make Path() build
        # WindowsPath with POSIX paths and crash (the exact failure in the
        # #140 CI run). The suppression path stays covered on Windows.
        pytest.skip("windows-only prompt section")
    _disallow(cfg)
    from backend.agent import loop

    prompt = loop._default_system_prompt(workspace="")
    # The tool list line must not offer screenshot...
    assert "powershell (Windows PowerShell), screenshot," not in prompt
    # ...and the sandbox/monitoring lines that name it generically are fine.


def test_system_prompt_keeps_screenshot_when_allowed(cfg):
    from backend.agent import loop

    if os.name != "nt":
        pytest.skip("windows-only prompt section")
    prompt = loop._default_system_prompt(workspace="")
    assert "powershell (Windows PowerShell), screenshot," in prompt
