"""Seam-level tests for the session-worktree operations (adr/0005).

These cross the new BindResult/SettleResult interface directly — the
lifecycle behavior is behavior-locked by test_worktrees.py and
test_agent.py; this file asserts what CALLERS get from the seam.
"""

import subprocess
from pathlib import Path

import pytest

from backend.agent import worktrees


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture(autouse=True)
def _clean_state():
    worktrees._chat_bindings.clear()
    worktrees._active.clear()
    yield
    worktrees._chat_bindings.clear()
    worktrees._active.clear()


async def test_bind_for_write_returns_unchanged_for_tools_without_isolation(repo: Path):
    for name in ("git_status", "git_push", "git_merge_back", "sandbox_test"):
        result = await worktrees.bind_for_write(
            str(repo), chat_id=f"60-{name}", tool_name=name, args={}
        )
        assert result.required is False, name
        assert result.workspace == str(repo), name
        assert not result.worktree, name
        assert not worktrees.binding_for(f"60-{name}"), name


async def test_bind_for_write_fresh_returns_note_and_event(repo: Path):
    result = await worktrees.bind_for_write(
        str(repo), chat_id="61", tool_name="write_file", args={}
    )
    assert result.required is True
    assert result.workspace == result.worktree
    assert result.reused is False
    assert result.worktree_id == "61"
    assert "# Workspace integration" in result.model_note
    assert result.branch.startswith("agent/")
    assert result.lifecycle, "fresh bind must carry the lifecycle event"
    await worktrees.release_session("61", why="test")


async def test_bind_for_write_reused_is_quiet(repo: Path):
    first = await worktrees.bind_for_write(str(repo), chat_id="62")
    second = await worktrees.bind_for_write(first.workspace, chat_id="62")
    assert second.workspace == first.workspace
    assert second.reused is True
    assert second.model_note == "", "reused binding must not re-emit a note"
    assert second.worktree == "", "reused binding must not re-fire the event"
    await worktrees.release_session("62", why="test")


async def test_bind_for_write_refusal_raises(repo: Path, tmp_path: Path):
    # A non-repo workspace with a second writer must refuse.
    nonrepo = tmp_path / "nonrepo"
    nonrepo.mkdir()
    await worktrees.bind_for_write(str(nonrepo), chat_id="63a")
    with pytest.raises(worktrees.IsolationRefused):
        await worktrees.bind_for_write(str(nonrepo), chat_id="63b")
    await worktrees.release_session("63a", why="test")


async def test_settle_session_drains_quiesced(repo: Path):
    wt = await worktrees.bind_for_write(str(repo), chat_id="64")
    assert not Path(wt.workspace).exists() or (Path(wt.workspace)).is_dir()
    settle = await worktrees.settle_session("64")
    assert settle.drained is True
    assert settle.status_note is None, "a drained session reports nothing"
    assert settle.commits_ahead == 0


async def test_settle_session_keeps_branch_with_work(repo: Path):
    wt = await worktrees.bind_for_write(str(repo), chat_id="65")
    wtp = Path(wt.workspace)
    (wtp / "work.md").write_text("work\n", encoding="utf-8")
    _git(wtp, "add", "-A")
    _git(wtp, "commit", "-q", "-m", "wip")
    settle = await worktrees.settle_session("65")
    assert settle.drained is False
    assert settle.commits_ahead == 1
    assert settle.status_note is not None
    assert settle.status_note["commits"] == 1
    assert settle.status_note["branch"] == settle.branch
    await worktrees.release_session("65", why="test")


async def test_settle_session_clean_dirty_no_commit_kept(repo: Path):
    wt = await worktrees.bind_for_write(str(repo), chat_id="66")
    (Path(wt.workspace) / "scratch.md").write_text("uncommitted\n", encoding="utf-8")
    settle = await worktrees.settle_session("66")
    assert settle.drained is False, "authored-looking dirt is never drained"
    assert settle.commits_ahead == 0
    assert settle.status_note is None, "no commits ahead — nothing to report"
    await worktrees.release_session("66", why="test")


async def test_settle_session_noop_when_not_isolated(repo: Path):
    settle = await worktrees.settle_session("67")
    assert settle.drained is False
    assert settle.status_note is None
    assert settle.branch == ""
