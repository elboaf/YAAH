"""Issue #169: a Settings toggle to enable/disable persistent memory.

Default is DISABLED (opt-in, owner decision 2026-09-30). When disabled:

- ``get_schemas`` never returns memory_save / memory_read / memory_delete;
- the system prompt carries no ``# Persistent memory`` block;
- a stray call returns a graceful pointer to the setting, never a traceback;
- the choice persists via the global config (``memory.enabled``).

The toggle gates, never deletes: on-disk stores under ``~/.yaah/memory/``
are untouched (never exercised here â no test writes to a real memory root).
"""

import pytest

from backend.agent import config, tools

_MEMORY_TOOLS = {"memory_save", "memory_read", "memory_delete"}


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    # Same reload-and-restore dance as the #140 suite: CONFIG_PATH is read
    # at import time, and every rebound attribute must be restored or later
    # tests read a config missing their keys.
    monkeypatch.setenv("YAAH_CONFIG_PATH", str(tmp_path / "config.json"))
    import importlib

    saved = dict(vars(config))
    importlib.reload(config)
    yield config
    monkeypatch.delenv("YAAH_CONFIG_PATH")
    for key, value in saved.items():
        setattr(config, key, value)


def _enable(c, enabled):
    c.save_config({"memory": {"enabled": enabled}})


def test_disabled_by_default(cfg):
    assert cfg.load_config()["memory"]["enabled"] is False


def test_schemas_exclude_memory_tools_by_default(cfg):
    names = {s["function"]["name"] for s in tools.get_schemas()}
    assert names & _MEMORY_TOOLS == set()


def test_schemas_include_memory_tools_when_enabled(cfg):
    _enable(cfg, True)
    names = {s["function"]["name"] for s in tools.get_schemas()}
    assert _MEMORY_TOOLS <= names


def test_stray_call_returns_graceful_pointer(cfg):
    _enable(cfg, False)
    import asyncio

    for name in sorted(_MEMORY_TOOLS):
        result = asyncio.run(tools.execute_tool(name, {}, workspace="."))
        text = str(result)
        assert "Traceback" not in text
        assert "Settings" in text


def test_memory_notes_suppressed_when_disabled(cfg, monkeypatch):
    from backend.agent import loop, memory

    monkeypatch.setattr(memory, "index_for_prompt", lambda ws: "IDX-BLOCK")
    _enable(cfg, False)
    assert loop._memory_notes(".") == ""


def test_memory_notes_flow_when_enabled(cfg, monkeypatch):
    from backend.agent import loop, memory

    monkeypatch.setattr(memory, "index_for_prompt", lambda ws: "IDX-BLOCK")
    _enable(cfg, True)
    assert loop._memory_notes(".") == "IDX-BLOCK"


def test_setting_persists(cfg):
    _enable(cfg, True)
    assert cfg.load_config()["memory"]["enabled"] is True


def test_settings_api_roundtrip(tmp_path, monkeypatch):
    """PUT /api/config merges the memory block: a save that only touches
    enabled must not wipe sibling keys of the stored block."""
    from fastapi.testclient import TestClient

    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    import backend.main as mainmod

    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    monkeypatch.setattr(mainmod, "save_config", cfgmod.save_config)
    from backend.main import app

    with TestClient(app) as client:
        r = client.put("/api/config", json={"memory": {"enabled": True}})
        assert r.status_code == 200
        cfg = cfgmod.load_config()
        assert cfg["memory"]["enabled"] is True
        got = client.get("/api/config").json()
        assert got["memory"]["enabled"] is True
