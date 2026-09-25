"""Interface-scale config: persistence + API clamp."""
import pytest
from fastapi.testclient import TestClient

from backend.agent.config import load_config, save_config


def test_ui_scale_persists(tmp_path, monkeypatch):
    """ui_scale rides the generic config merge without disturbing other settings."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("backend.main.CONFIG_PATH", tmp_path / "config.json", raising=False)

    save_config({"ui_scale": 1.25})
    assert load_config()["ui_scale"] == 1.25
    save_config({"max_steps": 50})
    # A later save that doesn't mention ui_scale must not reset it.
    assert load_config()["ui_scale"] == 1.25


def test_api_clamps_out_of_range_scale(tmp_path, monkeypatch):
    """PUT /api/config clamps ui_scale into [1.0, 1.5] — a wilder value
    would break the compact layout."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    # main.py re-imports load/save into its own namespace at module import.
    import backend.main as mainmod

    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    monkeypatch.setattr(mainmod, "save_config", cfgmod.save_config)
    from backend.main import app

    with TestClient(app) as client:
        r = client.put("/api/config", json={"ui_scale": 9.9})
        assert r.status_code == 200
        assert load_config()["ui_scale"] == 1.5
        r = client.put("/api/config", json={"ui_scale": 0.2})
        assert r.status_code == 200
        assert load_config()["ui_scale"] == 1.0
        r = client.get("/api/config")
        assert r.json()["ui_scale"] == 1.0
