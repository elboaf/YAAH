"""#329 (land-means-clean): post-run retirement of chat worktrees.

The lifecycle contract: a worktree exists only while its work is in
flight. When the work lands, the run worktree AND the chat worktree go
away — unless the chat tree is dirty or still holds run residue (those
survive and surface via the residue protocol). The agent never removes
the tree it stands in; ``retire_chat_worktree`` is harness code called
at run end (loop.py's finally block).
"""
import os
import subprocess
import time


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


async def _materialize(repo, cid=42):
    from backend.agent.worktrees import ensure_chat_worktree

    return await ensure_chat_worktree(str(repo), cid, "master")


async def test_retire_removes_clean_tree_and_frees_the_branch(tmp_path):
    """AC: 'everything created for that work goes away' + 'branches are
    never held open by finished work' — the retired tree vanishes from
    `git worktree list`, releasing its checkout."""
    from backend.agent.worktrees import retire_chat_worktree

    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]
    assert chat_dir.exists()
    assert chat_dir.as_posix() in _git(repo, "worktree", "list", "--porcelain")

    result = await retire_chat_worktree(str(repo), 42)
    assert result["retired"] is True, result
    assert not chat_dir.exists()
    assert chat_dir.as_posix() not in _git(repo, "worktree", "list", "--porcelain")


async def test_retire_keeps_dirty_tree(tmp_path):
    """'only dirty residue survives … never silently kept' — a dirty tree
    is not removed; the residue protocol surfaces it instead."""
    from backend.agent.worktrees import retire_chat_worktree

    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]
    (chat_dir / "uncommitted.txt").write_text("wip", encoding="utf-8")

    result = await retire_chat_worktree(str(repo), 42)
    assert result["retired"] is False
    assert result["reason"] == "dirty"
    assert chat_dir.exists()


async def test_retire_keeps_tree_with_run_residue(tmp_path):
    """The run namespace is invisible to status (#321), so residue is
    pinned by existence (#330): a tree holding `<chat>/run` survives."""
    from backend.agent.worktrees import retire_chat_worktree

    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]
    (chat_dir / "run").mkdir()  # residue: the run worktree dir remains

    result = await retire_chat_worktree(str(repo), 42)
    assert result["retired"] is False
    assert result["reason"] == "run residue present"
    assert chat_dir.exists()


async def test_retire_respects_min_age(tmp_path):
    """The rapid-turn guard: a tree younger than min_age_seconds is left
    standing (0 retires immediately — the post-run hook's choice)."""
    from backend.agent.worktrees import retire_chat_worktree

    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]

    result = await retire_chat_worktree(str(repo), 42, min_age_seconds=3600)
    assert result["retired"] is False
    assert "age" in result["reason"]
    assert chat_dir.exists()

    result_now = await retire_chat_worktree(str(repo), 42, min_age_seconds=0)
    assert result_now["retired"] is True
    assert not chat_dir.exists()


async def test_retire_no_worktree_is_a_clean_noop(tmp_path):
    from backend.agent.worktrees import retire_chat_worktree

    repo = _repo_with_commit(tmp_path)  # never materialized
    result = await retire_chat_worktree(str(repo), 4242)
    assert result == {"retired": False, "reason": "no chat worktree"}


async def test_retire_non_git_and_remote_are_noops(tmp_path):
    from backend.agent.worktrees import retire_chat_worktree

    plain = tmp_path / "plain"
    plain.mkdir()
    assert (await retire_chat_worktree(str(plain), 1))["retired"] is False
    assert (await retire_chat_worktree("remote://box/proj", 1))["retired"] is False
