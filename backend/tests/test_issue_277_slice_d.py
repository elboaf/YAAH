"""Slice D of #277 (ADR-0010): worktree maintenance + chip reads.

The sweeper retires clean chat worktrees of dead chats past the idle
threshold; an in-flight or residue run (nested run worktree present)
is never swept — pinned by existence since #321 hid `.scratch/` from
status (#330). Chip git-info reads follow the chat's
own worktree once it exists (ADR-0010 amendment: chip reads follow the
chat tree; before materialization they stay on the primary).
"""
import asyncio
import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _repo_with_commit(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


async def _register_workspace(repo):
    from backend.db.database import get_db

    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO workspaces (path, label) VALUES (?, ?)", (str(repo), "r")
        )
        await db.commit()
    finally:
        await db.close()


def _age(tree, days=40):
    old = time.time() - days * 24 * 3600
    os.utime(tree, (old, old))


@pytest.mark.asyncio
async def test_sweep_removes_old_clean_dead_chat_tree(tmp_path):
    from backend.agent import wt_sweep, worktrees

    repo = _repo_with_commit(tmp_path)
    await _register_workspace(repo)
    out = await worktrees.ensure_chat_worktree(str(repo), 999, "master")
    chat_dir = out["path"]
    assert chat_dir.exists()
    _age(chat_dir)

    result = await wt_sweep.sweep_stale_chat_worktrees()
    assert str(chat_dir) in result["swept"]
    assert not chat_dir.exists()


@pytest.mark.asyncio
async def test_sweep_keeps_live_chat_trees(tmp_path):
    from backend.db.database import create_conversation
    from backend.agent import wt_sweep, worktrees

    repo = _repo_with_commit(tmp_path)
    await _register_workspace(repo)
    cid = await create_conversation("t", str(repo))
    out = await worktrees.ensure_chat_worktree(str(repo), cid, "master")
    _age(out["path"])

    result = await wt_sweep.sweep_stale_chat_worktrees()
    assert out["path"].exists()  # a live chat's tree is never swept
    assert str(out["path"]) not in result["swept"]


@pytest.mark.asyncio
async def test_sweep_keeps_recent_trees(tmp_path):
    from backend.agent import wt_sweep, worktrees

    repo = _repo_with_commit(tmp_path)
    await _register_workspace(repo)
    out = await worktrees.ensure_chat_worktree(str(repo), 888, "master")
    # No aging: fresh tree stays regardless of cleanliness.
    result = await wt_sweep.sweep_stale_chat_worktrees()
    assert out["path"].exists()


@pytest.mark.asyncio
async def test_sweep_keeps_residue_trees(tmp_path):
    """A nested run worktree pins the tree even though #321 keeps
    `.scratch/` git-invisible — the run dir reads neither as untracked
    noise nor as anything at all in `status --porcelain`, so the pin is
    by EXISTENCE (#330): in-flight and residue runs are never swept."""
    from backend.agent import wt_sweep, worktrees

    repo = _repo_with_commit(tmp_path)
    await _register_workspace(repo)
    out = await worktrees.ensure_chat_worktree(str(repo), 777, "master")
    chat_dir = out["path"]
    # Residue: a nested run worktree inside the chat tree.
    nested = await worktrees.ensure_chat_worktree(str(chat_dir), 777, "master")
    assert nested["path"] is not None
    _age(chat_dir)

    result = await wt_sweep.sweep_stale_chat_worktrees()
    assert chat_dir.exists()
    assert str(chat_dir) not in result["swept"]


@pytest.mark.asyncio
async def test_sweep_keeps_dirty_untracked_files(tmp_path):
    """Plain untracked noise (not a run worktree) still pins via
    porcelain — the existence pin (#330) is additive, never a
    replacement for the dirty gate."""
    from backend.agent import wt_sweep, worktrees

    repo = _repo_with_commit(tmp_path)
    await _register_workspace(repo)
    out = await worktrees.ensure_chat_worktree(str(repo), 778, "master")
    chat_dir = out["path"]
    (chat_dir / "scratch-note.txt").write_text("user noise", encoding="utf-8")
    _age(chat_dir)

    result = await wt_sweep.sweep_stale_chat_worktrees()
    assert chat_dir.exists()
    assert str(chat_dir) not in result["swept"]


@pytest.mark.asyncio
async def test_sweep_is_gated_per_chat_not_per_workspace(tmp_path):
    """#329: a dead chat's clean tree sweeps even while OTHER chats of
    the same workspace are live — the old per-workspace gate made the
    sweeper a permanent no-op in the main workspace, where live chats
    always exist. The live chat's own tree is never touched."""
    from backend.db.database import create_conversation
    from backend.agent import wt_sweep, worktrees

    repo = _repo_with_commit(tmp_path)
    await _register_workspace(repo)
    live_id = await create_conversation("live chat", str(repo))
    live = await worktrees.ensure_chat_worktree(str(repo), live_id, "master")
    dead = await worktrees.ensure_chat_worktree(str(repo), 555, "master")
    _age(live["path"])
    _age(dead["path"])

    result = await wt_sweep.sweep_stale_chat_worktrees()
    assert live["path"].exists()
    assert str(live["path"]) not in result["swept"]
    assert not dead["path"].exists()
    assert str(dead["path"]) in result["swept"]


@pytest.mark.asyncio
async def test_sweep_retires_legacy_standalone_clone(tmp_path):
    """#329: a pre-#277 standalone clone (.git a directory, never
    worktree-registered) is invisible to `git worktree remove` — but a
    clean, dead, aged one is retired via plain removal all the same."""
    import uuid

    from backend.agent import wt_sweep, worktrees

    # Unique name: this file's tests share one process DB, so workspace
    # rows accumulate across tests — isolation comes from the path, not
    # the table.
    repo = _repo_with_commit(tmp_path, name=f"repo-{uuid.uuid4().hex[:8]}")
    await _register_workspace(repo)
    scratch = repo / ".scratch"
    clone = scratch / "chat-4711"
    _git(repo, "clone", "-q", str(repo), str(clone))
    assert (clone / ".git").is_dir()  # standalone: not worktree-registered
    assert str(clone) not in _git(repo, "worktree", "list", "--porcelain")
    _age(clone)

    result = await wt_sweep.sweep_stale_chat_worktrees()
    assert not clone.exists()
    assert str(clone) in result["swept"]


@pytest.mark.asyncio
async def test_sweep_keeps_non_chat_ids_alone(tmp_path):
    """Dirs under .scratch/ that are not per-chat worktrees (no numeric
    id) are never candidates — the chat-id parse is the gate."""
    import uuid

    from backend.agent import wt_sweep

    repo = _repo_with_commit(tmp_path, name=f"repo-{uuid.uuid4().hex[:8]}")
    await _register_workspace(repo)
    odd = repo / ".scratch" / "chat-mic-gate"
    odd.mkdir(parents=True)
    (odd / "f.txt").write_text("x", encoding="utf-8")
    _age(odd)

    result = await wt_sweep.sweep_stale_chat_worktrees()
    assert odd.exists()
    # Scope to THIS repo: workspace rows accumulate across the file's
    # tests (shared process DB), so global checked/kept counts include
    # earlier tests' candidates.
    mine = [p for p in result["swept"] if p.startswith(str(repo))]
    assert mine == []


@pytest.fixture()
def client():
    from backend.main import app

    with TestClient(app) as c:
        yield c


def test_git_info_follows_chat_worktree(client, tmp_path):
    from backend.agent import worktrees
    from backend.agent.gitinfo import invalidate_git_caches

    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    cid = client.post("/api/conversations", json={"workspace": str(repo)}).json()["id"]

    # Before materialization: the chip reads the primary (master).
    invalidate_git_caches(repo)
    info = client.get(f"/api/conversations/{cid}/git-info").json()["info"]
    assert info["branch"] == "master"

    # After: the chip follows the chat worktree (feature).
    asyncio.run(worktrees.ensure_chat_worktree(str(repo), cid, "feature"))
    invalidate_git_caches(repo)
    invalidate_git_caches(repo / ".scratch" / f"chat-{cid}")
    info = client.get(f"/api/conversations/{cid}/git-info").json()["info"]
    assert info["branch"] == "feature"
