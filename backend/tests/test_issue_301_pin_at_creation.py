"""Issue #301: pin at creation + lazy pin (ADR-0010 amendment, decisions 1-2).

Every chat in a local git workspace records selected_branch at creation:
the draft card's pick when one was made, else the workspace's
then-current branch stamped as an 'inherited' pin. The unset state
disappears — legacy NULL rows lazily pin on first read after upgrade,
and remote:/non-git workspaces keep no chip.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.db import database as db_mod


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _repo(tmp_path, name="repo", branch="master"):
    """A git repo with one commit, on the named branch."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A throwaway database, schema included."""
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "agent.db")
    return db_mod


async def _make_legacy_row(cid):
    """Simulate a pre-#301 row behind the migration's back."""
    import aiosqlite

    async with aiosqlite.connect(db_mod.DB_PATH) as d:
        await d.execute(
            "UPDATE conversations SET selected_branch = NULL,"
            " branch_pin_origin = NULL WHERE id = ?",
            (cid,),
        )
        await d.commit()


# ---- creation path ----


@pytest.mark.asyncio
async def test_no_pick_pins_workspace_branch_as_inherited(db, tmp_path):
    repo = _repo(tmp_path)
    cid = await db_mod.create_conversation("t", str(repo))
    conv = await db_mod.get_conversation(cid)
    assert conv["selected_branch"] == "master"
    assert conv["branch_pin_origin"] == "inherited"


@pytest.mark.asyncio
async def test_explicit_pick_still_wins_over_workspace_branch(db, tmp_path):
    repo = _repo(tmp_path)
    cid = await db_mod.create_conversation(
        "t", str(repo), selected_branch="feature", branch_pin_origin="explicit"
    )
    conv = await db_mod.get_conversation(cid)
    assert conv["selected_branch"] == "feature"
    assert conv["branch_pin_origin"] == "explicit"


@pytest.mark.asyncio
async def test_branch_without_origin_outside_git_still_derives_explicit(db):
    """The #302 derivation is unchanged: a non-git caller passing a branch
    without an origin still gets 'explicit' — nothing is resolved."""
    cid = await db_mod.create_conversation("t", None, selected_branch="feature")
    conv = await db_mod.get_conversation(cid)
    assert conv["selected_branch"] == "feature"
    assert conv["branch_pin_origin"] == "explicit"


@pytest.mark.asyncio
async def test_non_git_workspace_stays_unpinned(db, tmp_path):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    (bare / "readme.txt").write_text("x\n", encoding="utf-8")
    cid = await db_mod.create_conversation("t", str(bare))
    conv = await db_mod.get_conversation(cid)
    assert conv["selected_branch"] is None
    assert conv["branch_pin_origin"] is None


@pytest.mark.asyncio
async def test_remote_workspace_stays_unpinned(db, tmp_path):
    repo = _repo(tmp_path)
    cid = await db_mod.create_conversation("t", f"remote:dev:{repo}")
    conv = await db_mod.get_conversation(cid)
    assert conv["selected_branch"] is None
    assert conv["branch_pin_origin"] is None


@pytest.mark.asyncio
async def test_detached_head_is_not_pinned_as_a_sha(db, tmp_path):
    """A detached workspace reports a short SHA, which would be a pin born
    STALE — creation pins local branch names only."""
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    cid = await db_mod.create_conversation("t", str(repo))
    conv = await db_mod.get_conversation(cid)
    assert conv["selected_branch"] is None
    assert conv["branch_pin_origin"] is None


@pytest.mark.asyncio
async def test_no_workspace_stays_unpinned(db):
    cid = await db_mod.create_conversation("t", None)
    conv = await db_mod.get_conversation(cid)
    assert conv["selected_branch"] is None
    assert conv["branch_pin_origin"] is None


# ---- lazy pin: legacy NULL rows on first read ----


@pytest.mark.asyncio
async def test_legacy_null_row_lazily_pins_inherited(db, tmp_path):
    """A pre-#301 row (NULL selected_branch) pins the workspace's current
    branch as 'inherited' the first time anything reads it — there is no
    other sane value, and no blocking backfill is needed."""
    repo = _repo(tmp_path)
    cid = await db_mod.create_conversation("t", str(repo))
    # Simulate the legacy row behind the migration's back.
    await _make_legacy_row(cid)
    conv = await db_mod.get_conversation(cid)
    assert conv["selected_branch"] == "master"
    assert conv["branch_pin_origin"] == "inherited"


@pytest.mark.asyncio
async def test_lazy_pin_is_idempotent(db, tmp_path):
    """The second read must not overwrite what the first pinned — even
    after the workspace itself moves."""
    repo = _repo(tmp_path)
    cid = await db_mod.create_conversation("t", str(repo))
    await _make_legacy_row(cid)
    first = await db_mod.get_conversation(cid)
    assert first["selected_branch"] == "master"
    _git(repo, "checkout", "-q", "-b", "elsewhere")
    second = await db_mod.get_conversation(cid)
    assert second["selected_branch"] == "master"
    assert second["branch_pin_origin"] == "inherited"


@pytest.mark.asyncio
async def test_lazy_pin_skips_non_git_and_remote(db, tmp_path):
    bare = tmp_path / "plain"
    bare.mkdir()
    for ws in (str(bare), f"remote:dev:{tmp_path}", None, ""):
        cid = await db_mod.create_conversation("t", ws)
        await _make_legacy_row(cid)
        conv = await db_mod.get_conversation(cid)
        assert conv["selected_branch"] is None, ws
        assert conv["branch_pin_origin"] is None, ws


# ---- API surface ----


def test_api_create_pins_workspace_branch(tmp_path, monkeypatch):
    """POST /api/conversations without a branch pick: the row carries the
    workspace branch as an inherited pin; the response says so."""
    from fastapi.testclient import TestClient

    repo = _repo(tmp_path)
    from backend.main import app

    with TestClient(app) as client:
        r = client.post("/api/conversations", json={"workspace": str(repo)})
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        g = client.get(f"/api/conversations/{cid}/git-branch").json()
        assert g["branch"] == "master"
        assert g["pin_origin"] == "inherited"
        assert g["stale"] is False


# ---- branch note: origin-aware wording + the amendment rules ----


def test_note_inherited_pin_says_inherited_not_user_selected():
    """The note must not claim a selection the user never made."""
    from backend.agent import loop

    note = loop._selected_branch_note("master", 7, origin="inherited")
    assert "inherited" in note.lower()
    assert "The user selected branch" not in note


def test_note_explicit_pin_still_says_user_selected():
    from backend.agent import loop

    note = loop._selected_branch_note("bigtest", 7, origin="explicit")
    assert "The user selected branch" in note


def test_note_carries_amendment_rules():
    """Translation + start point + immunity: the note states that switching
    is a selector call (never a primary checkout), that new branches derive
    from the chat's own selection, and that a terminal git switch on the
    primary tree changes nothing about this chat's aim."""
    from backend.agent import loop

    note = loop._selected_branch_note("bigtest", 7)
    assert "branch_select" in note
    assert "never a checkout of the primary" in note
    assert "derive from" in note
    assert "git switch" in note


def test_degraded_note_inherited_pin_says_inherited():
    from backend.agent import loop

    note = loop._selected_branch_note_degraded("master", 7, "no repo", origin="inherited")
    assert "inherited" in note.lower()
    assert "The user selected branch" not in note


def test_stale_note_inherited_pin_says_inherited():
    from backend.agent import loop

    note = loop._selected_branch_note_stale("master", 7, origin="inherited")
    assert "inherited" in note.lower()
