"""Per-session context endpoint: must serialize, not leak a coroutine.

Regression for v0.11.0: get_context_window is async but was called without
await, so every GET /api/conversations/{id}/context 500'd with
"'coroutine' object is not iterable" — 2x/second per open session.
"""
from fastapi.testclient import TestClient


def test_context_endpoint_returns_json(tmp_path, monkeypatch):
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    import backend.main as mainmod

    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    from backend.main import app

    with TestClient(app) as client:
        conv_id = client.post("/api/conversations", json={}).json()["id"]
        r = client.get(f"/api/conversations/{conv_id}/context")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "context_tokens" in body
        assert "context_model" in body

    # And a missing conversation stays a clean 404.
    with TestClient(app) as client:
        r = client.get("/api/conversations/999999/context")
        assert r.status_code == 404
