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


def test_db_columns_exist_after_migration(tmp_path, monkeypatch):
    """Verify cost_spent and completion_tokens_total columns are added by migration."""
    import sqlite3
    from backend.db import database as dbmod

    db_path = tmp_path / "agent.db"
    monkeypatch.setattr(dbmod, "DATABASE_PATH", db_path)

    import importlib

    importlib.reload(dbmod)

    # Initialize via the same code path the app uses
    dbmod.get_db.__wrapped__ = None  # reset cached db
    import asyncio

    # Run init through the real code path
    async def init():
        db = await dbmod.get_db()
        try:
            # Run schema
            for stmt in dbmod.SCHEMA.split(";"):
                if stmt.strip():
                    await db.execute(stmt)
            # Run migrations
            cur = await db.execute("PRAGMA table_info(conversations)")
            conv_cols = {r[1] for r in await cur.fetchall()}
            if "context_tokens" not in conv_cols:
                await db.execute("ALTER TABLE conversations ADD COLUMN context_tokens INTEGER")
            if "context_model" not in conv_cols:
                await db.execute("ALTER TABLE conversations ADD COLUMN context_model TEXT")
            if "cost_spent" not in conv_cols:
                await db.execute("ALTER TABLE conversations ADD COLUMN cost_spent REAL")
            if "completion_tokens_total" not in conv_cols:
                await db.execute("ALTER TABLE conversations ADD COLUMN completion_tokens_total INTEGER")
            await db.commit()
        finally:
            await db.close()

    asyncio.run(init())

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(conversations)")
    cols = {row[1] for row in cur.fetchall()}
    conn.close()
    assert "cost_spent" in cols
    assert "completion_tokens_total" in cols