"""Tests for per-run file change snapshots."""

import pytest

from backend.agent.file_changes import diff_snapshots, snapshot_workspace, summarize_file_changes
from backend.tests.gitutil import run_git


@pytest.mark.asyncio
async def test_git_snapshot_reports_net_changes_in_subdirectory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    project = repo / "app"
    project.mkdir()
    outside = repo / "outside.txt"
    outside.write_text("unrelated\n", encoding="utf-8")
    (project / "edit.txt").write_text("old\n", encoding="utf-8")
    (project / "delete.txt").write_text("gone\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "baseline")

    before = await snapshot_workspace(str(project))
    (project / "edit.txt").write_text("new\nextra\n", encoding="utf-8")
    (project / "delete.txt").unlink()
    (project / "added.txt").write_text("hello\n", encoding="utf-8")
    # The agent only sees the workspace subtree; unrelated repository edits
    # must not be added to its run summary.
    outside.write_text("unrelated change\n", encoding="utf-8")
    after = await snapshot_workspace(str(project))

    rows = await diff_snapshots(before, after)
    assert {row["path"] for row in rows} == {"edit.txt", "delete.txt", "added.txt"}
    by_path = {row["path"]: row for row in rows}
    assert (by_path["edit.txt"]["added"], by_path["edit.txt"]["deleted"]) == (2, 1)
    assert (by_path["delete.txt"]["added"], by_path["delete.txt"]["deleted"]) == (0, 1)
    assert (by_path["added.txt"]["added"], by_path["added.txt"]["deleted"]) == (1, 0)
    assert summarize_file_changes(rows)["added"] == 3
    assert summarize_file_changes(rows)["deleted"] == 2


@pytest.mark.asyncio
async def test_non_git_snapshot_reports_binary_and_text_changes(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "note.txt").write_text("before\n", encoding="utf-8")
    before = await snapshot_workspace(str(root))
    (root / "note.txt").write_text("after\n", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\x00\x01")
    after = await snapshot_workspace(str(root))

    rows = await diff_snapshots(before, after)
    by_path = {row["path"]: row for row in rows}
    assert by_path["note.txt"]["added"] == 1
    assert by_path["note.txt"]["deleted"] == 1
    assert by_path["image.bin"]["binary"] is True
