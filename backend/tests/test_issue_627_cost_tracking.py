"""Issue #627: per-session cost tracking.

Tests the DB migration (cost_spent/completion_tokens_total columns) and
the context endpoint exposes the new cost fields.
"""
import sqlite3

from fastapi.testclient import TestClient


def test_context_endpoint_exposes_cost_fields():
    """The /context endpoint must include cost_spent and completion_tokens_total.
    Also validates the migration ran: a stale DB would 500 on the endpoint,
    and the PRAGMA below proves the columns exist.

    Runs against the conftest-redirected throwaway DB/config like every other
    suite test. The first version of this test reassigned CONFIG_PATH /
    DATABASE_PATH to tmp_path and importlib.reload()ed database + main to
    "get a fresh migration" — that poisoned the xdist worker for every later
    test sharing it (the test_remote device tests failed with KeyError
    'remote_devices' because the module graph now pointed at a deleted
    tmp_path). Never reload module graphs in tests; conftest's redirect is
    the fresh environment.
    """
    from backend.db import database as dbmod
    import backend.main as mainmod

    with TestClient(mainmod.app) as client:
        conv_id = client.post("/api/conversations", json={}).json()["id"]

        r = client.get(f"/api/conversations/{conv_id}/context")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "cost_spent" in body
        assert "completion_tokens_total" in body
        assert body["cost_spent"] is None
        assert body["completion_tokens_total"] is None

    # Migration ran: the conversations table carries the new columns on the
    # redirected throwaway DB — read via a bare connection, not a reloaded
    # module graph.
    conn = sqlite3.connect(dbmod.DB_PATH)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    finally:
        conn.close()
    assert {"cost_spent", "completion_tokens_total"} <= cols
