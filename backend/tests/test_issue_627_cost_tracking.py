"""Issue #627: per-session cost tracking.

Tests the DB migration (cost_spent/completion_tokens_total columns) and
the context endpoint exposes the new cost fields.
"""
from fastapi.testclient import TestClient


def test_context_endpoint_exposes_cost_fields(tmp_path, monkeypatch):
    """The /context endpoint must include cost_spent and completion_tokens_total.
    Also validates the migration ran (the endpoint fails on a stale DB)."""
    from backend.agent import config as cfgmod
    from backend.db import database as dbmod

    cfgmod.CONFIG_PATH = tmp_path / "config.json"
    dbmod.DATABASE_PATH = tmp_path / "agent.db"

    import backend.main as mainmod

    mainmod.load_config = cfgmod.load_config

    import importlib

    importlib.reload(dbmod)
    importlib.reload(mainmod)

    from backend.main import app

    with TestClient(app) as client:
        conv_id = client.post("/api/conversations", json={}).json()["id"]

        r = client.get(f"/api/conversations/{conv_id}/context")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "cost_spent" in body
        assert "completion_tokens_total" in body
        assert body["cost_spent"] is None
        assert body["completion_tokens_total"] is None