"""Tests for per-run Git activity summaries."""

import asyncio

import pytest

from backend.agent.git_activity import GitActivity, _outcome, _parse_git_command


def test_shell_recognizer_only_accepts_recognizable_mutating_git_commands():
    assert _parse_git_command("git checkout -b agent/task") == ("checkout", ["agent/task"], True)
    assert _parse_git_command("git push origin feature") == ("push", ["origin", "feature"], True)
    assert _parse_git_command("git status") is None
    assert _parse_git_command("git log && git commit -m done") is None
    assert _parse_git_command("echo check; git push") is None


def test_outcomes_distinguish_refusal_noop_and_cancelled():
    assert _outcome({"merged": False, "zero_commits": True}) == "no-op"
    assert _outcome({"merged": False, "reason": "merge refused"}) == "blocked"
    assert _outcome({"error": "tool stopped by user (run cancelled)"}) == "cancelled"
    assert _outcome({"exit_code": 1, "error": "fatal"}) == "failed"


def test_activity_keeps_operations_separate_from_cumulative_branch_context():
    activity = GitActivity("run-1")
    activity.set_context(
        "parent", "Agent", branch="agent/8/fix", base_branch="main",
        branch_action="reused", worktree="kept",
    )
    activity.set_settlement(
        "parent", "Agent", commits_ahead=3, dirty=True,
        worktree="kept", integrated=False,
    )
    activity.record(
        "parent", "Agent", "commit", "succeeded", source="agent-tool",
        branch="agent/8/fix", commit="abc123", subject="Fix the thing",
    )
    summary = activity.summary("completed")
    assert summary is not None
    lane = summary["lanes"][0]
    assert lane["commits_ahead"] == 3
    assert lane["branch_action"] == "reused"
    assert lane["operations"][0]["commit"] == "abc123"
    assert summary["run_id"] == "run-1"


@pytest.mark.asyncio
async def test_successful_merge_remains_integrated_after_final_worktree_settlement(monkeypatch, tmp_path):
    activity = GitActivity("run-merged")
    activity.set_context(
        "parent", "Agent", branch="agent/fix", base_branch="master",
        branch_action="created", worktree="kept",
    )
    monkeypatch.setattr(
        "backend.agent.git_activity._branch",
        lambda _ws: asyncio.sleep(0, result="master"),
    )
    await activity.record_tool(
        "parent", "Agent", "git_merge_back", {"branch": "agent/fix"},
        {"merged": True, "exit_code": 0, "branch": "agent/fix", "target_branch": "master"},
        str(tmp_path),
    )

    # turn_end observes that the just-merged session branch is now clean and
    # has no commits ahead; this is cleanup, not evidence that integration failed.
    activity.set_context("parent", "Agent", worktree="removed")
    activity.set_settlement(
        "parent", "Agent", commits_ahead=0, dirty=False,
        worktree="removed", integrated=False,
    )

    lane = activity.summary("completed")["lanes"][0]
    assert lane["integrated"] is True
    assert lane["commits_ahead"] == 0
    assert lane["worktree"] == "removed"


def test_no_activity_has_no_row():
    assert GitActivity("run-empty").summary("completed") is None


@pytest.mark.asyncio
async def test_record_tool_commit_uses_head_sha_and_subject(tmp_path, monkeypatch):
    activity = GitActivity("run-commit")
    monkeypatch.setattr("backend.agent.git_activity._branch", lambda _ws: asyncio.sleep(0, result="agent/task"))
    monkeypatch.setattr(
        "backend.agent.git_activity._commit_head",
        lambda _ws: asyncio.sleep(0, result={"commit": "a1b2c3", "subject": "Improve summary"}),
    )
    await activity.record_tool(
        "parent", "Agent", "git_commit", {"message": "Improve summary"},
        {"output": "[agent/task a1b2c3] Improve summary", "exit_code": 0}, str(tmp_path),
    )
    operation = activity.summary("completed")["lanes"][0]["operations"][0]
    assert operation["commit"] == "a1b2c3"
    assert operation["subject"] == "Improve summary"
