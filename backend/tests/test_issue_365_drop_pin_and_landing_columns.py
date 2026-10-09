"""#365: the isolation feature's columns die in the database, not just in code.

#361/#363 removed every reader and writer of the branch-pin columns
(conversations.selected_branch, conversations.branch_pin_origin) and the
landing columns (agents.landing_mode, agents.landing_branch), and the
fresh schema no longer creates them. A database created before the
removal still carries all four. This migration drops them in place on
first boot, so an upgraded database is column-for-column identical to a
fresh one and nothing of the dead design survives.
"""

import aiosqlite
import pytest

import backend.db.database as database

# The dead design's columns, per table.
_DEAD_CONV = ["selected_branch", "branch_pin_origin"]
_DEAD_AGENT = ["landing_mode", "landing_branch"]


async def test_drop_legacy_pin_and_landing_columns(monkeypatch, tmp_path):
    old_db = tmp_path / "legacy.db"
    async with aiosqlite.connect(old_db) as db:
        await db.executescript(database.SCHEMA)
        # The dead design's columns, as the pre-#359 schema had them.
        await db.execute("ALTER TABLE conversations ADD COLUMN selected_branch TEXT")
        await db.execute("ALTER TABLE conversations ADD COLUMN branch_pin_origin TEXT")
        await db.execute("ALTER TABLE agents ADD COLUMN landing_mode TEXT NOT NULL DEFAULT 'off'")
        await db.execute("ALTER TABLE agents ADD COLUMN landing_branch TEXT NOT NULL DEFAULT ''")
        # A row carrying dead-column data that must survive the drop.
        await db.execute(
            "INSERT INTO conversations (title, selected_branch, branch_pin_origin) "
            "VALUES ('old task', 'feature-x', 'explicit')"
        )
        await db.commit()

    monkeypatch.setattr(database, "DB_PATH", old_db)
    await database.init_db()
    db = await database.get_db()
    try:
        cur = await db.execute("PRAGMA table_info(conversations)")
        conv_cols = {r[1] for r in await cur.fetchall()}
        cur = await db.execute("PRAGMA table_info(agents)")
        agent_cols = {r[1] for r in await cur.fetchall()}

        # Dead columns gone from both tables...
        for col in _DEAD_CONV:
            assert col not in conv_cols
        for col in _DEAD_AGENT:
            assert col not in agent_cols

        # ...and the surviving columns match a fresh database exactly,
        # which is what "upgraded behaves like fresh" means here. The
        # fresh reference takes the SAME boot path (init_db/get_db), not
        # a bare executescript — get_db's own ALTERs are part of a fresh
        # database's shape.
        fresh_db_path = tmp_path / "fresh.db"
        monkeypatch.setattr(database, "DB_PATH", fresh_db_path)
        await database.init_db()
        fresh = await database.get_db()
        try:
            cur = await fresh.execute("PRAGMA table_info(conversations)")
            fresh_conv = {r[1] for r in await cur.fetchall()}
            cur = await fresh.execute("PRAGMA table_info(agents)")
            fresh_agent = {r[1] for r in await cur.fetchall()}
        finally:
            await fresh.close()
        assert conv_cols == fresh_conv
        assert agent_cols == fresh_agent

        # The pre-existing row survived; its live columns are intact.
        cur = await db.execute("SELECT title FROM conversations")
        assert (await cur.fetchone())["title"] == "old task"
    finally:
        await db.close()


@pytest.mark.parametrize("table,columns", [
    ("conversations", _DEAD_CONV),
    ("agents", _DEAD_AGENT),
])
async def test_fresh_schema_never_carries_the_dead_columns(table, columns):
    """The fresh schema must never regrow the dead design's columns."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "fresh.db"
        async with aiosqlite.connect(db_path) as db:
            await db.executescript(database.SCHEMA)
            cur = await db.execute(f"PRAGMA table_info({table})")
            cols = {r[1] for r in await cur.fetchall()}
            for col in columns:
                assert col not in cols
