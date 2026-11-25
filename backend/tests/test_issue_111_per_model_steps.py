"""Issue #111: max steps become per-model (config.model_steps), resolved
by the agent loop as model entry > provider entry > legacy global > default."""
import pytest
from fastapi.testclient import TestClient

from backend.agent.config import load_config, save_config


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    import backend.main as mainmod

    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    monkeypatch.setattr(mainmod, "save_config", cfgmod.save_config)
    from backend.main import app

    with TestClient(app) as c:
        yield c


def test_model_steps_persists_via_api(client):
    """PUT /api/config merges model_steps per model id and GET returns it."""
    r = client.put(
        "/api/config",
        json={"model_steps": {"acme::big-model": 50, "acme::small-model": 0}},
    )
    assert r.status_code == 200
    r = client.get("/api/config")
    steps = r.json()["model_steps"]
    assert steps["acme::big-model"] == 50
    assert steps["acme::small-model"] == 0
    # A later save without model_steps must not wipe existing entries.
    client.put("/api/config", json={"ui_scale": 1.1})
    assert load_config()["model_steps"]["acme::big-model"] == 50


def test_model_steps_rejects_non_numeric(client):
    """Non-numeric / negative entries are dropped, not persisted raw."""
    r = client.put(
        "/api/config",
        json={"model_steps": {"acme::m": 30, "acme::bad": "lots", "acme::neg": -5}},
    )
    assert r.status_code == 200
    steps = load_config()["model_steps"]
    assert steps["acme::m"] == 30
    assert "acme::bad" not in steps
    assert "acme::neg" not in steps


def test_resolve_max_steps_model_wins():
    """Precedence: per-model entry > provider entry > global > default."""
    from backend.agent.loop import _resolve_max_steps

    cfg = {
        "max_steps": 100,
        "providers": {"acme": {"max_steps": 150}},
        "model_steps": {"acme::m1": 40},
    }
    assert _resolve_max_steps(cfg, "acme::m1") == 40
    assert _resolve_max_steps(cfg, "acme::m2") == 150  # provider entry
    assert _resolve_max_steps({}, "") == 200  # shipped default
    # 0 is honored (unlimited) at every level.
    assert _resolve_max_steps({"providers": {"acme": {"max_steps": 0}}}, "acme::x") == 0
    assert _resolve_max_steps({"model_steps": {"acme::x": 0}}, "acme::x") == 0
    # Bad values fall back to the shipped default.
    assert _resolve_max_steps({"max_steps": "nonsense"}, "x") == 200

def test_default_model_per_model_entry_honored():
    """CodeRabbit finding: when no model override is given (default turn),
    the per-model entry saved for the active default model must be honored,
    not just the provider-level entry. The user sets 'max steps' on the
    default model in Settings -> Providers, so that must be what runs."""
    from backend.agent.loop import _resolve_max_steps

    cfg = {
        "active_provider": "acme",
        "model": "acme::default-model",
        "providers": {
            "acme": {"model": "acme::default-model", "max_steps": 150}
        },
        "model_steps": {"acme::default-model": 40}
    }
    # No override = default model turn
    assert _resolve_max_steps(cfg, None) == 40
    assert _resolve_max_steps(cfg, "") == 40
    # Explicit override of the same model also gets 40
    assert _resolve_max_steps(cfg, "acme::default-model") == 40
    # A different model without its own entry falls back to provider level
    assert _resolve_max_steps(cfg, "acme::other-model") == 150
