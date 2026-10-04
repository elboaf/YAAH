"""Slice A of #277 (ADR-0010): per-chat worktree materialization and the
run-resolution seam.

`ensure_chat_worktree` materializes `.scratch/chat-<id>/` at the chat's
selected branch (attached when git allows, detached at the tip when the
branch is checked out in the primary - the master-pin case), idempotently.
`run_agent` re-points `turn_workspace` at it, so every tool resolves inside
the chat's private tree and the primary worktree is untouched by agents.
"""
import subprocess

import pytest

from backend.agent import worktrees


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _repo_with_commit(tmp_path, name="repo", branch="master"):
    """A clean git repo with one commit on `branch` and a configured identity."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


@pytest.mark.asyncio
async def test_materializes_at_selected_branch_attached(tmp_path):
    repo = _repo_with_commit(tmp_path)
    # master is checked out in the primary here; use a second branch so
    # attachment is possible.
    _git(repo, "branch", "feature")
    out = await worktrees.ensure_chat_worktree(str(repo), 7, "feature")
    assert out["path"] == repo / ".scratch" / "chat-7"
    assert out["created"] is True
    assert out["detached"] is False
    # The chat worktree really is on the selected branch.
    head = (repo / ".scratch" / "chat-7" / ".git").read_text(encoding="utf-8")
    target = head.strip().removeprefix("gitdir:").strip()
    from pathlib import Path

    gitdir = Path(target)
    if not gitdir.is_absolute():
        gitdir = (repo / ".scratch" / "chat-7" / gitdir).resolve()
    assert "ref: refs/heads/feature" in (gitdir / "HEAD").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_detaches_when_branch_held_by_primary(tmp_path):
    repo = _repo_with_commit(tmp_path)  # master checked out in the "primary"
    out = await worktrees.ensure_chat_worktree(str(repo), 8, "master")
    assert out["path"] == repo / ".scratch" / "chat-8"
    assert out["detached"] is True
    # Same tip as the selected branch: nothing was lost by detaching.
    tip = _git(repo, "rev-parse", "master")
    wtip = _git(repo / ".scratch" / "chat-8", "rev-parse", "HEAD")
    assert tip == wtip


@pytest.mark.asyncio
async def test_idempotent_second_call_reports_existing(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    first = await worktrees.ensure_chat_worktree(str(repo), 9, "feature")
    assert first["created"] is True
    second = await worktrees.ensure_chat_worktree(str(repo), 9, "feature")
    assert second["created"] is False
    assert second["path"] == first["path"]


@pytest.mark.asyncio
async def test_skips_without_branch_or_git(tmp_path):
    repo = _repo_with_commit(tmp_path)
    # No branch picked: nothing materializes.
    out = await worktrees.ensure_chat_worktree(str(repo), 10, "")
    assert out["path"] is None
    # Remote namespace: out of scope v1 (ADR-0010).
    out = await worktrees.ensure_chat_worktree("remote:box:C:\\proj", 11, "master")
    assert out["path"] is None
    # Not a git repo: explicit error, not a crash.
    plain = tmp_path / "plain"
    plain.mkdir()
    out = await worktrees.ensure_chat_worktree(str(plain), 12, "master")
    assert out["path"] is None and out.get("error")


@pytest.mark.asyncio
async def test_run_agent_repoints_turn_workspace(tmp_path, monkeypatch):
    """The run itself executes in the chat worktree: the file tool the loop
    would call resolves from the worktree, not the primary."""
    from backend.db.database import create_conversation, update_conversation
    from backend.agent import loop

    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    monkeypatch.setattr(loop, "_agents_notes", lambda ws: "")
    monkeypatch.setattr(loop, "_memory_notes", lambda ws: "")
    captured = {}

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        captured["system"] = messages[0]["content"]

        async def _stream():
            yield {"type": "content", "text": "done"}
            yield {"type": "finish"}
        return _stream()

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    cid = await create_conversation("t")
    await update_conversation(cid, selected_branch="feature")

    async for _ in loop.run_agent(cid, "go", str(repo)):
        pass

    # The worktree exists and the prompt teaches the run-worktree SOP
    # relative to the CHAT namespace, not the primary.
    assert (repo / ".scratch" / f"chat-{cid}" / ".git").exists()
    assert f".scratch/chat-{cid}/run" in captured["system"]
    assert "per-chat worktree" in captured["system"]
