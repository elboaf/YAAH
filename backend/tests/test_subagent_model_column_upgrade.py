"""Regression: upgrading a pre-#383 database crashed backend startup.

rc.9's boot (main.py lifespan: get_db -> repair_bare_model_scopes) died
with `sqlite3.OperationalError: no such column: subagent_model` on every
database upgraded from an earlier release: the conversations ALTER that
adds the column was nested inside the
`if "model" not in conv_cols or "effort" not in conv_cols` guard, and an
upgraded database already has both columns — so the block was skipped and
the one-time boot repair's SELECT on subagent_model had no column to
query. The agents-table ALTER sat at top level and worked; conversations
did not. Fresh databases were unaffected (SCHEMA creates the column).
"""

import aiosqlite
import pytest

import backend.db.database as database


@pytest.fixture(autouse=True)
def _reset_one_time_flags():
    """The boot repair is once-per-process by design; this test needs a
    fresh pass (it drives its own DB path, so get_db's migration guard
    does not need resetting)."""
    database._repair_scope_done = False
    database._stamp_scope_done = False
    yield
    database._repair_scope_done = False
    database._stamp_scope_done = False


async def _fake_list_all_models(providers):
    return {"providers": {"openai": {"models": ["gpt-5.2"]}}}


def _make_pre_383_db(path):
    """Write an rc.8-era database: today's schema minus both #383 columns
    (which is exactly what an upgraded install carries), plus one row with
    a bare model id so the boot repair has real work to do."""
    async def build():
        async with aiosqlite.connect(path) as db:
            await db.executescript(database.SCHEMA)
            # The rc.9 trigger already names subagent_model; drop it so the
            # column drop below leaves an honest pre-#383 shape behind.
            await db.execute(
                "DROP TRIGGER IF EXISTS conversations_remote_revision_update"
            )
            await db.execute("ALTER TABLE conversations DROP COLUMN subagent_model")
            await db.execute("ALTER TABLE agents DROP COLUMN subagent_model")
            await db.execute(
                "INSERT INTO conversations (title, workspace, model, effort)"
                " VALUES ('pre-upgrade chat', NULL, 'gpt-5.2', '')"
            )
            await db.commit()
    return build()


async def test_boot_migration_adds_column_and_repair_survives(monkeypatch, tmp_path):
    """The user's exact crash: lifespan boot on an upgraded database."""
    import backend.agent.providers as providers_mod

    old_db = tmp_path / "upgraded.db"
    await _make_pre_383_db(old_db)
    monkeypatch.setattr(database, "DB_PATH", old_db)
    monkeypatch.setattr(providers_mod, "list_all_models", _fake_list_all_models)

    # The exact boot sequence from main.py's lifespan. Before the fix the
    # repair raises OperationalError here and FastAPI refuses to start.
    await database.get_db()
    await database.repair_bare_model_scopes()

    db = await database.get_db()
    try:
        cur = await db.execute("PRAGMA table_info(conversations)")
        conv_cols = {r[1] for r in await cur.fetchall()}
        cur = await db.execute("PRAGMA table_info(agents)")
        agent_cols = {r[1] for r in await cur.fetchall()}
        assert "subagent_model" in conv_cols
        assert "subagent_model" in agent_cols

        # The pre-upgrade row survived and the repair did the job it used
        # to crash doing: the bare model id is provider-qualified.
        cur = await db.execute("SELECT model FROM conversations")
        assert (await cur.fetchone())["model"] == "openai::gpt-5.2"

        # #383, part 2: the conversations revision trigger's UPDATE OF
        # list is code-owned. An upgraded database must pick up the new
        # trigger body — IF NOT EXISTS would leave the pre-#383 body in
        # place, so subagent_model writes would never bump the revision
        # counter.
        cur = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger'"
            " AND name = 'conversations_remote_revision_update'"
        )
        trig = await cur.fetchone()
        assert "subagent_model" in (trig["sql"] or "")
        cur = await db.execute(
            "SELECT remote_revision_counter FROM conversations"
            " WHERE model = 'openai::gpt-5.2'"
        )
        before = (await cur.fetchone())["remote_revision_counter"]
        await db.execute(
            "UPDATE conversations SET subagent_model = 'openai::gpt-5'"
            " WHERE model = 'openai::gpt-5.2'"
        )
        await db.commit()
        cur = await db.execute(
            "SELECT remote_revision_counter FROM conversations"
            " WHERE model = 'openai::gpt-5.2'"
        )
        after = (await cur.fetchone())["remote_revision_counter"]
        assert after == before + 1
    finally:
        await db.close()


async def test_upgraded_columns_match_fresh(monkeypatch, tmp_path):
    """'Upgraded behaves like fresh' (the #365 contract): any new column
    added inside a guard that an upgraded database skips breaks this
    equality — which is exactly how the nested conversations ALTER
    slipped through."""
    upgraded = tmp_path / "upgraded.db"
    await _make_pre_383_db(upgraded)
    monkeypatch.setattr(database, "DB_PATH", upgraded)
    db = await database.get_db()
    try:
        cur = await db.execute("PRAGMA table_info(conversations)")
        upgraded_conv = {r[1] for r in await cur.fetchall()}
        cur = await db.execute("PRAGMA table_info(agents)")
        upgraded_agents = {r[1] for r in await cur.fetchall()}
    finally:
        await db.close()

    fresh = tmp_path / "fresh.db"
    monkeypatch.setattr(database, "DB_PATH", fresh)
    db = await database.get_db()
    try:
        cur = await db.execute("PRAGMA table_info(conversations)")
        fresh_conv = {r[1] for r in await cur.fetchall()}
        cur = await db.execute("PRAGMA table_info(agents)")
        fresh_agents = {r[1] for r in await cur.fetchall()}
    finally:
        await db.close()

    assert upgraded_conv == fresh_conv
    assert upgraded_agents == fresh_agents