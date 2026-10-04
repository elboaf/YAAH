"""Slice B of #277 (ADR-0010): the agent-facing branch selector tool.

`branch_select` creates the branch when missing - derived from the chat's
currently selected branch, never the primary's HEAD (server-enforced
start point) - and stores the pick. Refuses while the chat's worktree
has uncommitted changes (dirty-flip guard). The primary worktree is
never checked out or moved.
"""
import subprocess

import pytest

from backend.agent.tools import branch_select


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


async def _conv_with_worktree(tmp_path, repo, branch="master", dirty=False):
    from backend.db.database import create_conversation, update_conversation
    from backend.agent import worktrees

    cid = await create_conversation("t")
    await update_conversation(cid, selected_branch=branch)
    out = await worktrees.ensure_chat_worktree(str(repo), cid, branch)
    assert out["path"] is not None, out
    if dirty:
        (out["path"] / "stray.txt").write_text("uncommitted\n", encoding="utf-8")
    return cid


@pytest.mark.asyncio
async def test_selects_existing_branch(tmp_path):
    from backend.db.database import create_conversation, get_conversation

    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    cid = await create_conversation("t")
    out = await branch_select(str(repo), branch="feature", conversation_id=cid)
    assert out["ok"] is True
    assert (await get_conversation(cid))["selected_branch"] == "feature"


@pytest.mark.asyncio
async def test_creates_missing_branch_from_current_selection(tmp_path):
    from backend.db.database import create_conversation, get_conversation

    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "base")
    cid = await create_conversation("t")
    from backend.db.database import update_conversation

    await update_conversation(cid, selected_branch="base")
    out = await branch_select(str(repo), branch="next", conversation_id=cid)
    assert out["ok"] is True and out["created"] is True
    # Start point was the chat's selection, not the primary's HEAD:
    # give base its own commit and prove next derives from it.
    assert _git(repo, "rev-parse", "next") == _git(repo, "rev-parse", "base")
    assert (await get_conversation(cid))["selected_branch"] == "next"


@pytest.mark.asyncio
async def test_dirty_flip_guard_refuses(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    cid = await _conv_with_worktree(tmp_path, repo, branch="master", dirty=True)
    before = await branch_select(str(repo), branch="feature", conversation_id=cid)
    assert before["ok"] is False
    assert "uncommitted" in before["error"]
    # The selection did not move.
    from backend.db.database import get_conversation

    assert (await get_conversation(cid))["selected_branch"] == "master"


@pytest.mark.asyncio
async def test_clean_worktree_flip_succeeds(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    cid = await _conv_with_worktree(tmp_path, repo, branch="master", dirty=False)
    out = await branch_select(str(repo), branch="feature", conversation_id=cid)
    assert out["ok"] is True


@pytest.mark.asyncio
async def test_refuses_when_create_false_and_missing(tmp_path):
    from backend.db.database import create_conversation

    repo = _repo_with_commit(tmp_path)
    cid = await create_conversation("t")
    out = await branch_select(str(repo), branch="ghost", create=False, conversation_id=cid)
    assert out["ok"] is False and "not a local branch" in out["error"]


@pytest.mark.asyncio
async def test_refuses_empty_or_optionish_names(tmp_path):
    from backend.db.database import create_conversation

    repo = _repo_with_commit(tmp_path)
    cid = await create_conversation("t")
    for bad in ("", "-b"):
        out = await branch_select(str(repo), branch=bad, conversation_id=cid)
        assert out["ok"] is False


@pytest.mark.asyncio
async def test_tool_declared_and_classified():
    from backend.agent.tools import get_schemas, tool_risk

    names = {s["function"]["name"] for s in get_schemas()}
    assert "branch_select" in names
    assert tool_risk("branch_select") == "mutating"
