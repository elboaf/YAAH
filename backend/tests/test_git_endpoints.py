"""Status-strip git endpoints: git-info readout, branch list, and the
whitelisted git-command executor (checkout / status / commit / push).

The command endpoint is the UI's way to run git without an agent turn; its
results must land in the conversation as synthetic tool rows that survive
reload but never reach model context (load_history drops orphaned tool rows).
"""
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.agent.gitinfo import invalidate_git_caches


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


@pytest.fixture()
def client():
    from backend.main import app

    with TestClient(app) as c:
        yield c


def _conversation(client, workspace):
    return client.post("/api/conversations", json={"workspace": str(workspace)}).json()["id"]


def _info(client, conv_id, repo):
    """git-info with the module cache dropped (the TTL cache is correct app
    behavior; tests change the repo behind its back)."""
    invalidate_git_caches(repo)
    r = client.get(f"/api/conversations/{conv_id}/git-info")
    assert r.status_code == 200, r.text
    return r.json()["info"]


def test_git_info_clean_repo(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    r = client.get(f"/api/conversations/{conv_id}/git-info")
    assert r.status_code == 200, r.text
    info = r.json()["info"]
    assert info["branch"] == "master"
    assert info["dirty"] is False
    assert info["added"] == 0 and info["deleted"] == 0
    assert info["ahead"] == 0 and info["behind"] == 0
    assert info["remote_hash"] is None  # no upstream configured
    assert info["local_hash"], "short hash present"


def test_git_info_dirty_counts_and_untracked(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    (repo / "hello.txt").write_text("hi\nmore\n", encoding="utf-8")  # +1 line
    (repo / "untracked.txt").write_text("x", encoding="utf-8")
    info = client.get(f"/api/conversations/{conv_id}/git-info").json()["info"]
    assert info["dirty"] is True
    assert info["added"] == 1 and info["deleted"] == 0
    assert info["untracked"] == 1
    assert info["changed"] == 2  # both files


def test_git_info_ahead_behind_with_upstream(client, tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    repo = _repo_with_commit(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "master")
    conv_id = _conversation(client, repo)
    info = client.get(f"/api/conversations/{conv_id}/git-info").json()["info"]
    assert info["upstream"] == "origin/master"
    assert info["remote_hash"] == info["local_hash"]
    assert (info["ahead"], info["behind"]) == (0, 0)

    # A local commit ahead of the upstream shows in the pair.
    (repo / "second.txt").write_text("2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    info = _info(client, conv_id, repo)
    assert info["ahead"] == 1 and info["behind"] == 0
    assert info["remote_hash"] != info["local_hash"]


def test_git_info_non_repo_returns_none(client, tmp_path):
    empty = tmp_path / "notarepo"
    empty.mkdir()
    conv_id = _conversation(client, empty)
    r = client.get(f"/api/conversations/{conv_id}/git-info")
    assert r.status_code == 200
    assert r.json()["info"] is None


def test_git_info_missing_conversation_404(client):
    assert client.get("/api/conversations/999999/git-info").status_code == 404


def test_git_branches_lists_local(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    conv_id = _conversation(client, repo)
    r = client.get(f"/api/conversations/{conv_id}/git-branches")
    assert r.status_code == 200, r.text
    assert r.json()["branches"] == ["feature", "master"]


def test_workspace_git_branches_and_checkout_before_conversation(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")

    r = client.get("/api/workspaces/git-branches", params={"workspace": str(repo)})
    assert r.status_code == 200, r.text
    assert r.json() == {"branch": "master", "branches": ["feature", "master"]}

    r = client.post(
        "/api/workspaces/git-checkout",
        json={"workspace": str(repo), "branch": "feature"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert _git(repo, "branch", "--show-current") == "feature"

    info = client.get(
        "/api/workspaces/git-branches", params={"workspace": str(repo)}
    ).json()
    assert info["branch"] == "feature"


def test_workspace_git_endpoints_hide_nonrepo_default_and_remote(client, tmp_path):
    empty = tmp_path / "empty-workspace"
    empty.mkdir()
    assert client.get(
        "/api/workspaces/git-branches", params={"workspace": str(empty)}
    ).json() == {"branch": None, "branches": []}
    assert client.get(
        "/api/workspaces/git-branches", params={"workspace": ""}
    ).json() == {"branch": None, "branches": []}
    assert client.get(
        "/api/workspaces/git-branches", params={"workspace": "remote:host"}
    ).json() == {"branch": None, "branches": []}


def test_workspace_git_checkout_surfaces_dirty_tree_refusal(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    _git(repo, "checkout", "feature")
    (repo / "hello.txt").write_text("feature version\n", encoding="utf-8")
    _git(repo, "add", "hello.txt")
    _git(repo, "commit", "-q", "-m", "feature change")
    _git(repo, "checkout", "master")
    (repo / "hello.txt").write_text("uncommitted version\n", encoding="utf-8")

    r = client.post(
        "/api/workspaces/git-checkout",
        json={"workspace": str(repo), "branch": "feature"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    assert "local changes" in r.json()["error"].lower()
    assert _git(repo, "branch", "--show-current") == "master"


def test_workspace_git_checkout_rejects_empty_and_option_like_branch(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    for branch in ("", "--detach"):
        r = client.post(
            "/api/workspaces/git-checkout",
            json={"workspace": str(repo), "branch": branch},
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is False
        assert r.json().get("error")
    assert _git(repo, "branch", "--show-current") == "master"


def test_git_command_checkout_switches_branch(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    conv_id = _conversation(client, repo)
    r = client.post(
        f"/api/conversations/{conv_id}/git-command", json={"action": "checkout", "branch": "feature"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    info = client.get(f"/api/conversations/{conv_id}/git-info").json()["info"]
    assert info["branch"] == "feature"


def test_git_command_commit_stages_all_and_posts_trace_row(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    (repo / "hello.txt").write_text("hi\nmore\n", encoding="utf-8")
    (repo / "new.txt").write_text("n", encoding="utf-8")

    r = client.post(
        f"/api/conversations/{conv_id}/git-command",
        json={"action": "commit", "message": "ui commit"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert "ui commit" in r.json()["output"]

    # Both files landed (stage-all semantics).
    assert "more" in (repo / "hello.txt").read_text(encoding="utf-8")
    assert (repo / "new.txt").exists()
    info = client.get(f"/api/conversations/{conv_id}/git-info").json()["info"]
    assert info["dirty"] is False

    # The command is recorded as a synthetic tool row.
    msgs = client.get(f"/api/conversations/{conv_id}/messages").json()
    row = msgs[-1]
    assert row["role"] == "tool"
    assert row["tool_call_id"].startswith("ui-git-commit-")
    assert row["tool_calls"][0]["function"]["name"] == "git commit"
    assert "ui commit" in json.loads(row["content"])["output"]


def test_git_command_commit_requires_message(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    r = client.post(f"/api/conversations/{conv_id}/git-command", json={"action": "commit", "message": "  "})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "commit message is empty"}


def test_git_command_status(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    (repo / "dirty.txt").write_text("d", encoding="utf-8")
    r = client.post(f"/api/conversations/{conv_id}/git-command", json={"action": "status"})
    assert r.status_code == 200, r.text
    assert "?? dirty.txt" in r.json()["output"]


def test_git_command_push_without_upstream_falls_back(client, tmp_path):
    """Bare push on an upstream-less repo fails; the endpoint retries with
    --set-upstream and records the note (the push itself fails again here —
    origin points nowhere — but the fallback path is what's under test)."""
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    r = client.post(f"/api/conversations/{conv_id}/git-command", json={"action": "push"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False  # origin does not exist
    assert body.get("note") == "set upstream to origin/master"
    msgs = client.get(f"/api/conversations/{conv_id}/messages").json()
    row = msgs[-1]
    assert row["tool_call_id"].startswith("ui-git-push-")


def test_git_command_whitelist_rejects_arbitrary_actions(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    r = client.post(
        f"/api/conversations/{conv_id}/git-command", json={"action": "push", "branch": "--force; rm -rf"}
    )
    # 'push' is whitelisted; the payload is ignored. Arbitrary actions are not.
    assert r.json()["ok"] is False or r.json().get("ok") is False
    r2 = client.post(f"/api/conversations/{conv_id}/git-command", json={"action": "reset --hard"})
    assert r2.status_code == 400


def test_git_command_non_repo_workspace(client, tmp_path):
    """A non-repo workspace still answers (git's own 'not a repository'
    error) — the UI hides the whole cluster for non-repos, so this path is
    defense-in-depth, not a UX surface."""
    empty = tmp_path / "empty"
    empty.mkdir()
    conv_id = _conversation(client, empty)
    r = client.post(f"/api/conversations/{conv_id}/git-command", json={"action": "status"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "not a git repository" in body["error"]
