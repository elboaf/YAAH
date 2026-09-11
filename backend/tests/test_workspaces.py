"""Workspace registry: migration, CRUD, relocation, API shape."""
import os

import pytest
from httpx import ASGITransport, AsyncClient

from backend.db.database import (
    create_conversation,
    delete_workspace,
    get_workspace_by_path,
    list_conversations,
    list_workspaces,
    migrate_workspaces,
    upsert_workspace,
)


@pytest.mark.asyncio
async def test_migration_normalizes_legacy_and_seeds_registry():
    # Legacy rows: '' and '.' both mean "no root directory" -> NULL (Default).
    legacy_dot = await create_conversation("dot", ".")
    legacy_empty = await create_conversation("empty", "")
    filed = await create_conversation("filed", "C:\\proj\\Alpha")

    from backend.db.database import get_db

    db = await get_db()
    try:
        await migrate_workspaces(db)
    finally:
        await db.close()

    convs = {c["id"]: c for c in await list_conversations()}
    assert convs[legacy_dot]["workspace"] is None
    assert convs[legacy_empty]["workspace"] is None
    assert convs[filed]["workspace"] == "C:\\proj\\Alpha"

    rows = await list_workspaces()
    by_path = {r["path"]: r for r in rows}
    assert None in by_path, "Default row must exist"
    assert by_path[None]["label"] == "Default"
    assert "C:\\proj\\Alpha" in by_path
    assert by_path["C:\\proj\\Alpha"]["label"] == "Alpha"


@pytest.mark.asyncio
async def test_migration_is_idempotent():
    await create_conversation("task", "C:\\proj\\Beta")
    from backend.db.database import get_db

    db = await get_db()
    try:
        await migrate_workspaces(db)
        await migrate_workspaces(db)
        await migrate_workspaces(db)
    finally:
        await db.close()
    rows = await list_workspaces()
    paths = [r["path"] for r in rows]
    assert paths.count("C:\\proj\\Beta") == 1
    assert paths.count(None) == 1


@pytest.mark.asyncio
async def test_upsert_dedupes_case_and_slashes(tmp_path):
    real = tmp_path / "Repo"
    real.mkdir()
    a = await upsert_workspace(str(real))
    # Same folder, different case + trailing slash + redundant segment.
    b = await upsert_workspace(
        str(real).lower() + "\\" + ".." + "\\" + real.name
    )
    assert a["id"] == b["id"], "case/slash variants must dedupe to one row"
    rows = await list_workspaces()
    assert sum(1 for r in rows if r["path"] and r["path"].lower() == str(real).lower()) == 1


@pytest.mark.asyncio
async def test_delete_workspace_relocates_conversations_to_default():
    ws = await upsert_workspace("C:\\proj\\Gone")
    cid = await create_conversation("orphan", ws["path"])
    default = await get_workspace_by_path(None)

    result = await delete_workspace(ws["id"])
    assert result["relocated"] == 1

    convs = await list_conversations()
    mine = next(c for c in convs if c["id"] == cid)
    assert mine["workspace"] is None

    paths = [r["path"] for r in await list_workspaces()]
    assert "C:\\proj\\Gone" not in paths
    assert None in paths  # Default survives


@pytest.mark.asyncio
async def test_delete_default_is_rejected():
    default = await get_workspace_by_path(None)
    with pytest.raises(ValueError):
        await delete_workspace(default["id"])


@pytest.mark.asyncio
async def test_workspaces_api_list_add_delete(tmp_path):
    real = tmp_path / "ApiWs"
    real.mkdir()
    transport = ASGITransport(app=__import__("backend.main", fromlist=["app"]).app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # add (resolved + deduped)
        r = await client.post("/api/workspaces", json={"path": str(real)})
        assert r.status_code == 200
        ws = r.json()
        assert os.path.normcase(ws["path"]) == os.path.normcase(str(real))
        assert ws["label"] == "ApiWs"
        assert ws["exists"] is True

        # list includes exists + conversation_count
        r = await client.get("/api/workspaces")
        rows = {w["path"]: w for w in r.json()}
        assert rows[ws["path"]]["exists"] is True
        assert rows[ws["path"]]["conversation_count"] == 0

        # file a conversation, then remove: relocation count surfaces
        await create_conversation("t", ws["path"])
        r = await client.delete(f"/api/workspaces/{ws['id']}")
        assert r.status_code == 200
        assert r.json()["relocated"] == 1

        # Default cannot be removed
        default = await get_workspace_by_path(None)
        r = await client.delete(f"/api/workspaces/{default['id']}")
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_dead_path_listed_with_missing_marker():
    await upsert_workspace("C:\\definitely\\not\\a\\real\\folder\\xyz")
    rows = await list_workspaces()
    dead = next(r for r in rows if r["path"] and "xyz" in r["path"])
    assert dead["exists"] is False
