"""Issue #290: run-in-flight state for the status-strip badge.

runwatch derives which .scratch/chat-<id>/ run worktrees exist and each
one's residue state (clean / dirty / unmerged) from polled git state —
no agent announcements. The endpoint serves it to the chip; the residue
protocol decides what a run start may auto-remove.
"""
import asyncio
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.agent.runwatch import classify_run_residue, invalidate_run_caches


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _repo_with_commit(tmp_path, name="repo"):
    """A clean git repo with one commit and a configured identity."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


def _add_run_worktree(repo, chat_id, leaf="run", branch=None, base="master"):
    """A worktree in the deterministic per-chat namespace, as the prompt
    contract prescribes."""
    branch = branch or f"run/chat-{chat_id}"
    path = repo / ".scratch" / f"chat-{chat_id}" / leaf
    _git(repo, "worktree", "add", "-b", branch, str(path), base)
    return path


def _runs(repo, target=None):
    invalidate_run_caches(repo)
    return asyncio.run(run_worktrees(repo, target))


from backend.agent.runwatch import run_worktrees  # noqa: E402


@pytest.fixture()
def client():
    from backend.main import app

    with TestClient(app) as c:
        yield c


def _conversation(client, workspace):
    return client.post("/api/conversations", json={"workspace": str(workspace)}).json()["id"]


# ---------------------------------------------------------------- namespace


def test_lists_chat_namespace_run_worktrees(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _add_run_worktree(repo, 7)
    runs = _runs(repo)
    assert [r["branch"] for r in runs] == ["run/chat-7"]
    assert runs[0]["chat_id"] == "7"
    assert runs[0]["leaf"] == "run"
    assert str(repo / ".scratch" / "chat-7" / "run") == runs[0]["path"]


def test_excludes_primary_and_non_chat_worktrees(tmp_path):
    repo = _repo_with_commit(tmp_path)
    # Legacy pre-#290 naming stays invisible to the chip (it predates the
    # namespace contract), and the primary worktree itself never lists.
    _git(repo, "worktree", "add", "-b", "run-20260101-legacy",
         str(repo / ".scratch" / "run-20260101-legacy"), "master")
    runs = _runs(repo)
    assert runs == []


def test_run_leaf_variants_attribute_to_the_chat(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _add_run_worktree(repo, 3)
    _add_run_worktree(repo, 3, leaf="run-2", branch="run/chat-3-residue")
    runs = _runs(repo)
    assert sorted(r["leaf"] for r in runs) == ["run", "run-2"]
    assert all(r["chat_id"] == "3" for r in runs)


# ------------------------------------------------------------ residue logic


def test_classify_maps_states():
    # (dirty, merged) -> residue. The pure core of the protocol: merged
    # + clean auto-clears; dirty or unmerged surfaces.
    assert classify_run_residue(False, True) == "clean"
    assert classify_run_residue(True, True) == "dirty"
    assert classify_run_residue(False, False) == "unmerged"
    assert classify_run_residue(True, False) == "dirty"


def test_residue_clean_when_landed_and_forgotten(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _add_run_worktree(repo, 7)  # no commits, no changes, merged into base
    runs = _runs(repo, target="master")
    assert [(r["branch"], r["residue"]) for r in runs] == [("run/chat-7", "clean")]


def test_residue_dirty_with_uncommitted_changes(tmp_path):
    repo = _repo_with_commit(tmp_path)
    path = _add_run_worktree(repo, 7)
    (path / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    runs = _runs(repo, target="master")
    assert [r["residue"] for r in runs] == ["dirty"]


def test_residue_unmerged_with_commits(tmp_path):
    repo = _repo_with_commit(tmp_path)
    path = _add_run_worktree(repo, 7)
    (path / "done.txt").write_text("committed\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "run work")
    runs = _runs(repo, target="master")
    assert [r["residue"] for r in runs] == ["unmerged"]


def test_unmerged_against_selected_branch_not_master(tmp_path):
    """Residue is judged against the chat's selected branch — the landing
    target — not against master."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "checkout", "-q", "-b", "bigtest")
    _git(repo, "worktree", "add", "-b", "run/chat-9",
         str(repo / ".scratch" / "chat-9" / "run"), "bigtest")
    path = repo / ".scratch" / "chat-9" / "run"
    (path / "done.txt").write_text("committed\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "run work")
    # Against master the run is ahead (unmerged); once bigtest contains
    # the run's commit, the work has landed on its target and is clean.
    assert [r["residue"] for r in _runs(repo, target="master")] == ["unmerged"]
    _git(repo, "merge", "-q", "--ff-only", "run/chat-9")
    assert [r["residue"] for r in _runs(repo, target="bigtest")] == ["clean"]


# ---------------------------------------------------------------- endpoint


def test_endpoint_serves_runs_with_target(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    _add_run_worktree(repo, conv_id)
    r = client.get(f"/api/conversations/{conv_id}/run-worktrees")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target"] == "master"
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["branch"] == f"run/chat-{conv_id}"
    assert run["chat_id"] == str(conv_id)
    assert run["residue"] == "clean"


def test_endpoint_prefers_stored_selection_as_target(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    _add_run_worktree(repo, conv_id)
    client.post(f"/api/conversations/{conv_id}/branch-select",
                json={"branch": "master"})
    body = client.get(f"/api/conversations/{conv_id}/run-worktrees").json()
    assert body["target"] == "master"


def test_endpoint_empty_without_runs(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    body = client.get(f"/api/conversations/{conv_id}/run-worktrees").json()
    assert body == {"runs": [], "target": "master"}


def test_endpoint_non_workspace_returns_empty(client, tmp_path):
    empty = tmp_path / "notarepo"
    empty.mkdir()
    conv_id = _conversation(client, empty)
    body = client.get(f"/api/conversations/{conv_id}/run-worktrees").json()
    assert body == {"runs": [], "target": None}


def test_endpoint_missing_conversation_404(client):
    assert client.get("/api/conversations/999999/run-worktrees").status_code == 404
