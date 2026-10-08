"""Workspace registry: migration, CRUD, relocation, API shape."""
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from backend.db.database import (
    create_conversation,
    delete_workspace,
    get_workspace_by_path,
    list_conversations,
    list_workspaces,
    migrate_workspaces,
    reorder_workspaces,
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
    # Case-folded dedupe is universal by design (the registry can hold
    # Windows-style paths while running anywhere), as is slash redundancy.
    b = await upsert_workspace(str(real).lower())
    assert a["id"] == b["id"], "case variants must dedupe"
    c = await upsert_workspace(str(real) + os.sep + ".." + os.sep + real.name)
    assert a["id"] == c["id"], "slash variants must dedupe to one row"
    rows = await list_workspaces()
    assert (
        sum(
            1
            for r in rows
            if r["path"] and str(Path(r["path"]).resolve()) == str(real.resolve())
        )
        == 1
    )


@pytest.mark.asyncio
async def test_reorder_workspaces_stamps_positions_42():
    ws_a = await upsert_workspace("C:\\proj\\OrdA")
    ws_b = await upsert_workspace("C:\\proj\\OrdB")
    ws_c = await upsert_workspace("C:\\proj\\OrdC")
    default = await get_workspace_by_path(None)

    await reorder_workspaces([ws_c["id"], ws_a["id"], ws_b["id"]])

    rows = {r["id"]: r for r in await list_workspaces()}
    assert rows[ws_c["id"]]["position"] == 0
    assert rows[ws_a["id"]]["position"] == 1
    assert rows[ws_b["id"]]["position"] == 2
    # Default is always first in the sidebar and never needs a position.
    assert rows[default["id"]]["position"] is None


@pytest.mark.asyncio
async def test_reorder_workspaces_leaves_unlisted_rows_unordered_42():
    """A workspace the user never dragged stays NULL ('never manually
    ordered') and must not inherit a stale id-order backfill."""
    ws_a = await upsert_workspace("C:\\proj\\ReoA")
    ws_b = await upsert_workspace("C:\\proj\\ReoB")

    await reorder_workspaces([ws_a["id"]])

    rows = {r["id"]: r for r in await list_workspaces()}
    assert rows[ws_a["id"]]["position"] == 0
    assert rows[ws_b["id"]]["position"] is None


@pytest.mark.asyncio
async def test_reorder_api_persists_order_42(tmp_path):
    real = tmp_path / "ReoApi"
    real.mkdir()
    ws = await upsert_workspace(str(real))
    transport = ASGITransport(app=__import__("backend.main", fromlist=["app"]).app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/workspaces/reorder", json={"ordered_ids": [ws["id"]]}
        )
        assert r.status_code == 200
        rows = {w["id"]: w for w in (await client.get("/api/workspaces")).json()}
        assert rows[ws["id"]]["position"] == 0

        # Unknown ids are ignored, not 404 — the sidebar sends its full list.
        r2 = await client.post(
            "/api/workspaces/reorder", json={"ordered_ids": [ws["id"], 999999]}
        )
        assert r2.status_code == 200


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


@pytest.mark.asyncio
async def test_add_workspace_creates_missing_folder(tmp_path):
    """Typed-in paths (remote 'add folder on host') get created, not
    registered as permanent missing ghosts."""
    target = tmp_path / "new" / "deep" / "proj"
    from httpx import ASGITransport, AsyncClient

    from backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/workspaces", json={"path": str(target)})
    body = res.json()
    assert target.is_dir()
    assert body["exists"] is True

@pytest.mark.asyncio
async def test_typed_paths_normalize_against_home(monkeypatch, tmp_path):
    """`~` expands and bare names anchor to the home directory — a typed
    `test` must not try to create a folder in the backend's root-owned
    install cwd (the remote 'add folder on host' failure)."""
    import os
    from httpx import ASGITransport, AsyncClient

    from backend.main import app

    real_home = os.path.expanduser("~")
    monkeypatch.setattr(
        os.path, "expanduser",
        lambda p: str(tmp_path) + p[1:] if p == "~" or p.startswith("~/") else p,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/workspaces", json={"path": "typedproj"})
        row = res.json()
        assert (tmp_path / "typedproj").is_dir()
        assert row["exists"] is True

        res2 = await client.post("/api/workspaces", json={"path": "~/typedtilde"})
        assert (tmp_path / "typedtilde").is_dir()
        assert res2.json()["path"] == str(tmp_path / "typedtilde")


# ---------------------------------------------- the thin switch tool (#361)


async def _switch_repo(tmp_path):
    """A small git repo on a branch, for direct tool-level tests."""
    import subprocess

    repo = tmp_path / "switch-repo"
    repo.mkdir()
    def g(*a):
        proc = subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True)
        assert proc.returncode == 0, f"git {a}: {proc.stderr}"
        return proc.stdout.strip()
    g("init", "-q", "-b", "master")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "T")
    (repo / "f.txt").write_text("hi\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "first")
    return repo


@pytest.mark.asyncio
async def test_branch_select_tool_switches_and_creates(tmp_path):
    """The executor behind the endpoint, called directly: plain checkout of
    an existing branch, and -b-style creation of a missing one."""
    import subprocess

    from backend.agent.tools import branch_select

    repo = await _switch_repo(tmp_path)
    subprocess.run(["git", "branch", "feature"], cwd=repo, capture_output=True)

    r = await branch_select(workspace=str(repo), branch="feature")
    assert r == {"ok": True, "branch": "feature", "created": False}

    r2 = await branch_select(workspace=str(repo), branch="fresh", create=False)
    assert r2 == {"ok": False, "error": "not a local branch: fresh"}

    r3 = await branch_select(workspace=str(repo), branch="fresh")
    assert r3["ok"] is True and r3["created"] is True
    cur = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
    )
    assert cur.stdout.strip() == "fresh"


@pytest.mark.asyncio
async def test_branch_select_tool_surfaces_git_refusals_verbatim(tmp_path):
    """No harness guard survives: an uncommitted change that git refuses to
    clobber comes back as the tool error, verbatim in shape."""
    import subprocess

    from backend.agent.tools import branch_select

    repo = await _switch_repo(tmp_path)
    subprocess.run(["git", "branch", "feature"], cwd=repo, capture_output=True)
    # make hello.txt differ between the branches, then dirty it here
    subprocess.run(["git", "checkout", "-q", "feature"], cwd=repo, capture_output=True)
    (repo / "f.txt").write_text("feature content\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "feature content"], cwd=repo, capture_output=True)
    subprocess.run(["git", "checkout", "-q", "master"], cwd=repo, capture_output=True)
    (repo / "f.txt").write_text("uncommitted master edit\n", encoding="utf-8")

    r = await branch_select(workspace=str(repo), branch="feature")
    assert r["ok"] is False
    assert r["error"]
    cur = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
    )
    assert cur.stdout.strip() == "master"  # unmoved
