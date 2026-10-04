"""Slice E of #277 (ADR-0010): selector flips move the chat worktree.

ensure_chat_worktree on an existing clean tree retargets it in place -
flip = `git checkout` inside the chat's own private worktree, never the
primary (ADR-0010 branch-selector decision). Attached trees switch to
the branch; when git refuses (the branch is checked out elsewhere),
they detach at the branch's tip - the same one-checkout fallback as
creation.
"""
import subprocess

import pytest


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


def _branch_of(chat_dir):
    head = _git(chat_dir, "symbolic-ref", "-q", "HEAD")
    return head.removeprefix("refs/heads/") if head else ""


@pytest.mark.asyncio
async def test_flip_attached_tree_follows_new_selection(tmp_path):
    from backend.agent import worktrees

    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    _git(repo, "branch", "topic")
    # feature is free (the primary holds master) -> attached creation.
    out = await worktrees.ensure_chat_worktree(str(repo), 5, "feature")
    assert out["detached"] is False and _branch_of(out["path"]) == "feature"

    # The user flips the chat's selector to topic; the next run
    # retargets the existing tree in place.
    out2 = await worktrees.ensure_chat_worktree(str(repo), 5, "topic")
    assert out2["path"] == out["path"]
    assert out2["detached"] is False
    assert _branch_of(out["path"]) == "topic"


@pytest.mark.asyncio
async def test_flip_to_primary_held_branch_detaches(tmp_path):
    from backend.agent import worktrees

    repo = _repo_with_commit(tmp_path)  # master lives in the primary
    _git(repo, "branch", "feature")
    out = await worktrees.ensure_chat_worktree(str(repo), 6, "feature")
    assert out["detached"] is False

    # Flip to master: git refuses (the primary holds it), so the tree
    # detaches at master's tip - identical commit, nothing lost.
    out2 = await worktrees.ensure_chat_worktree(str(repo), 6, "master")
    assert out2["detached"] is True
    assert _git(out["path"], "rev-parse", "HEAD") == _git(repo, "rev-parse", "master")


@pytest.mark.asyncio
async def test_flip_back_reattaches_when_branch_free(tmp_path):
    from backend.agent import worktrees

    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    first = await worktrees.ensure_chat_worktree(str(repo), 7, "master")
    # master is held by the primary -> detached
    assert first["detached"] is True
    second = await worktrees.ensure_chat_worktree(str(repo), 7, "feature")
    assert second["detached"] is False
    # Back to master: still held by the primary -> detached again, at tip.
    third = await worktrees.ensure_chat_worktree(str(repo), 7, "master")
    assert third["detached"] is True
    assert _git(first["path"], "rev-parse", "HEAD") == _git(repo, "rev-parse", "master")
