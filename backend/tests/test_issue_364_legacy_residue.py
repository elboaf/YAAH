r"""#364: the one-time legacy residue cleanup pass.

The per-chat worktree design is gone (#360-#362); what it left standing
- registered chat worktrees under <workspace>/.scratch/chat-<id>/,
  legacy standalone clones, and merged run/* / wip/* refs - must be
  healed exactly once at boot. The pass follows the old sweeper's
  (wt_sweep.py, deleted at #361) test shape: real temp git repos, the
  pass run against them, asserts on the returned report plus
  filesystem/git state.

The test process shares one database (conftest.py), so every test
starts by dropping the marker row (autouse fixture) and all report
asserts are scoped to this test's own repo paths - earlier tests'
registered repos are re-walked by later passes and must not interfere.
"""
import asyncio
import subprocess
import uuid

import pytest


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _repo_with_commit(tmp_path, name=None):
    repo = tmp_path / (name or f"repo-{uuid.uuid4().hex[:8]}")
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    # #321 in the old design: the .scratch namespace is git-invisible,
    # so a nested run worktree never reads as the chat tree's dirt.
    (repo / ".gitignore").write_text(".scratch/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    # Mirror the ADR-0010 arrangement: the primary tree sits on its own
    # branch so the chat tree can hold the selected one.
    _git(repo, "checkout", "-q", "-b", "dev")
    return repo


async def _register_workspace(repo):
    from backend.db.database import get_db

    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO workspaces (path, label) VALUES (?, ?)",
            (str(repo), "r"),
        )
        await db.commit()
    finally:
        await db.close()


def _materialize_worktree(repo, cid=42):
    """A registered chat worktree, as the dead design created it. The
    chat tree holds `master`; the primary sits on `dev`."""
    chat_dir = repo / ".scratch" / f"chat-{cid}"
    _git(repo, "worktree", "add", str(chat_dir), "master")
    return chat_dir


def _branch_names(repo):
    return _git(repo, "branch", "--list", "--format=%(refname:short)").splitlines()


@pytest.fixture(autouse=True)
def _fresh_marker():
    """Each test runs the pass fresh: the process-wide database keeps
    the marker row from earlier tests, so drop it first."""
    from backend.db.database import get_db

    async def _drop():
        db = await get_db()
        try:
            await db.execute(
                "DELETE FROM app_meta WHERE key = 'legacy_residue_cleanup'"
            )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(_drop())


# --------------------------------------------------------------- trees


async def test_clean_registered_worktree_is_removed(tmp_path):
    from backend.agent.legacy_residue import run_legacy_residue_cleanup

    repo = _repo_with_commit(tmp_path)
    chat_dir = _materialize_worktree(repo)
    assert f"chat-42" in _git(repo, "worktree", "list")
    await _register_workspace(repo)

    result = await run_legacy_residue_cleanup()

    assert str(chat_dir) in result["removed_trees"], result
    assert not chat_dir.exists()
    # The branch checkout was released: no worktree holds it anymore.
    assert "chat-42" not in _git(repo, "worktree", "list")


async def test_dirty_tree_is_kept_and_surfaced(tmp_path):
    from backend.agent.legacy_residue import run_legacy_residue_cleanup

    repo = _repo_with_commit(tmp_path)
    chat_dir = _materialize_worktree(repo)
    (chat_dir / "draft.txt").write_text("wip", encoding="utf-8")
    await _register_workspace(repo)

    result = await run_legacy_residue_cleanup()

    assert chat_dir.exists()
    assert result["removed_trees"] == []
    entry = next(e for e in result["surfaced"] if e["path"] == str(chat_dir))
    assert "dirty" in entry["reason"]


async def test_dirty_nested_run_worktree_blocks_parent_removal(tmp_path):
    """The #342 nest, as live residue: <chat>/.scratch/chat-<id>/run is
    a registered run worktree with uncommitted work. Removing the parent
    would delete that work with it - the parent must be surfaced."""
    from backend.agent.legacy_residue import run_legacy_residue_cleanup

    repo = _repo_with_commit(tmp_path)
    chat_dir = _materialize_worktree(repo)
    nested = chat_dir / ".scratch" / "chat-42" / "run"
    _git(chat_dir, "worktree", "add", str(nested), "HEAD")
    (nested / "uncommitted.txt").write_text("x", encoding="utf-8")
    await _register_workspace(repo)

    result = await run_legacy_residue_cleanup()

    assert chat_dir.exists()
    entry = next(e for e in result["surfaced"] if e["path"] == str(chat_dir))
    assert "run" in entry["reason"]


async def test_clean_nested_run_worktree_does_not_block(tmp_path):
    from backend.agent.legacy_residue import run_legacy_residue_cleanup

    repo = _repo_with_commit(tmp_path)
    chat_dir = _materialize_worktree(repo)
    nested = chat_dir / ".scratch" / "chat-42" / "run"
    _git(chat_dir, "worktree", "add", str(nested), "HEAD")
    await _register_workspace(repo)

    result = await run_legacy_residue_cleanup()

    assert str(chat_dir) in result["removed_trees"], result
    assert not chat_dir.exists()


async def test_legacy_standalone_clone_is_removed(tmp_path):
    """Pre-#277 residue: a plain clone, `.git` a directory - git's
    `worktree remove` refuses it, read-bit-aware rmtree retires it."""
    from backend.agent.legacy_residue import run_legacy_residue_cleanup

    repo = _repo_with_commit(tmp_path)
    scratch = repo / ".scratch"
    scratch.mkdir()
    chat_dir = scratch / "chat-77"
    _git(repo, "clone", "-q", str(repo), str(chat_dir))
    assert (chat_dir / ".git").is_dir()
    await _register_workspace(repo)

    result = await run_legacy_residue_cleanup()

    assert str(chat_dir) in result["removed_trees"], result
    assert not chat_dir.exists()


async def test_non_chat_and_bad_id_dirs_are_ignored(tmp_path):
    from backend.agent.legacy_residue import run_legacy_residue_cleanup

    repo = _repo_with_commit(tmp_path)
    scratch = repo / ".scratch"
    scratch.mkdir()
    (scratch / "chat-mic-gate").mkdir()
    (scratch / "unrelated").mkdir()
    await _register_workspace(repo)

    result = await run_legacy_residue_cleanup()

    assert (scratch / "chat-mic-gate").exists()
    assert (scratch / "unrelated").exists()
    assert result["removed_trees"] == []


# ------------------------------------------------------------ branches


async def test_merged_run_branch_reaped_unmerged_kept(tmp_path):
    from backend.agent.legacy_residue import run_legacy_residue_cleanup

    repo = _repo_with_commit(tmp_path)
    chat_dir = _materialize_worktree(repo)
    # Landed run branch: its commit is reachable from master.
    _git(chat_dir, "checkout", "-q", "-b", "run/fix-something-42")
    (chat_dir / "work.txt").write_text("landed\n", encoding="utf-8")
    _git(chat_dir, "add", "-A")
    _git(chat_dir, "commit", "-q", "-m", "land")
    _git(repo, "merge", "--no-ff", "-q", "-m", "landed", "run/fix-something-42")
    # Unmerged run branch: a commit no other local branch contains.
    _git(chat_dir, "checkout", "-q", "-b", "run/orphan-42")
    (chat_dir / "orphan.txt").write_text("x\n", encoding="utf-8")
    _git(chat_dir, "add", "-A")
    _git(chat_dir, "commit", "-q", "-m", "orphan")
    _git(chat_dir, "checkout", "-q", "master")
    await _register_workspace(repo)

    result = await run_legacy_residue_cleanup()

    assert "run/fix-something-42" in result["reaped_branches"], result
    assert "run/orphan-42" in result["kept_branches"], result
    names = _branch_names(repo)
    assert "run/fix-something-42" not in names
    assert "run/orphan-42" in names


async def test_wip_branches_follow_the_same_rule(tmp_path):
    from backend.agent.legacy_residue import run_legacy_residue_cleanup

    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "wip/landed-idea", "master")
    # An unmerged wip branch: one commit contained by NO local branch.
    _git(repo, "checkout", "-q", "--detach", "master")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "orphan wip")
    _git(repo, "branch", "wip/orphan-idea")
    _git(repo, "checkout", "-q", "dev")
    await _register_workspace(repo)

    result = await run_legacy_residue_cleanup()

    assert "wip/landed-idea" in result["reaped_branches"], result
    assert "wip/orphan-idea" in result["kept_branches"], result
    names = _branch_names(repo)
    assert "wip/landed-idea" not in names
    assert "wip/orphan-idea" in names


async def test_run_branch_merged_into_dev_is_reaped(tmp_path):
    """`--contains` measures against every local branch, not one pinned
    landing target (#343's constraint dies with the design): landed
    into dev - the primary's branch - still reaps."""
    from backend.agent.legacy_residue import run_legacy_residue_cleanup

    repo = _repo_with_commit(tmp_path)
    chat_dir = _materialize_worktree(repo)
    _git(chat_dir, "checkout", "-q", "-b", "run/into-dev-42")
    (chat_dir / "d.txt").write_text("d\n", encoding="utf-8")
    _git(chat_dir, "add", "-A")
    _git(chat_dir, "commit", "-q", "-m", "land in dev")
    _git(repo, "checkout", "-q", "dev")
    _git(repo, "merge", "--no-ff", "-q", "-m", "landed", "run/into-dev-42")
    _git(repo, "checkout", "-q", "master")
    await _register_workspace(repo)

    result = await run_legacy_residue_cleanup()

    assert "run/into-dev-42" in result["reaped_branches"], result
    assert "run/into-dev-42" not in _branch_names(repo)


# -------------------------------------------------------------- marker


async def test_marker_row_prevents_second_run(tmp_path):
    """The pass's once-guard is the database row itself: after the first
    run, newly built residue survives a second call untouched."""
    from backend.agent import legacy_residue
    from backend.db.database import get_db

    repo = _repo_with_commit(tmp_path)
    chat_dir = _materialize_worktree(repo)
    await _register_workspace(repo)

    first = await legacy_residue.run_legacy_residue_cleanup()
    assert str(chat_dir) in first["removed_trees"], first
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT value FROM app_meta WHERE key = 'legacy_residue_cleanup'"
        )
        row = await cur.fetchone()
    finally:
        await db.close()
    assert row is not None and row["value"] == "done"

    # Fresh residue appears after the pass; a second call (a second
    # boot) must be a no-op.
    second_tree = _materialize_worktree(repo, cid=99)
    second = await legacy_residue.run_legacy_residue_cleanup()

    assert second == {"skipped": True}, second
    assert second_tree.exists()


async def test_db_unavailable_reports_error_and_sets_no_marker(
    tmp_path, monkeypatch
):
    import aiosqlite

    import backend.db.database as dbmod
    from backend.agent import legacy_residue

    real_path = dbmod.DB_PATH
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setattr(dbmod, "DB_PATH", blocker / "agent.db")

    result = await legacy_residue.run_legacy_residue_cleanup()

    assert result == {"error": "db unavailable"}, result
    # Nothing was recorded: the next boot retries.
    async with aiosqlite.connect(real_path) as db:
        cur = await db.execute(
            "SELECT count(*) FROM app_meta WHERE key = 'legacy_residue_cleanup'"
        )
        (count,) = await cur.fetchone()
    assert count == 0
