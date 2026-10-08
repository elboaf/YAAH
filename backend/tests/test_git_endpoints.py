"""Status-strip git endpoints: git-info readout, branch list, and the
whitelisted git-command executor (checkout / status / commit / push).

The direct world (#359/ADR-0017): there is ONE tree - the workspace
checkout - and every endpoint reads or acts on it. The command endpoint is
the UI's way to run git without an agent turn; its results land in the
conversation as synthetic tool rows that survive reload but never reach
model context (load_history drops orphaned tool rows).
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


def _divergent_content(repo, branch_with_commit):
    """Make hello.txt differ between the two branches: commit one version on
    branch_with_commit, so an uncommitted master edit on the other branch
    blocks the checkout."""
    _git(repo, "branch", branch_with_commit) if branch_with_commit not in _git(repo, "branch", "--list").split() else None
    _git(repo, "checkout", branch_with_commit)
    (repo / "hello.txt").write_text("uncommitted master edit\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "feature content")
    _git(repo, "checkout", "master")


def _conversation(client, workspace):
    return client.post("/api/conversations", json={"workspace": str(workspace)}).json()["id"]


class _FakeHostSession:
    """A host whose repository is a real local repo the test prepared;
    git commands ship through exec_tool and run against it (the wire
    shape is run_bash's: {exit_code, output, ...})."""

    host_id = "h-fake"

    def __init__(self, host_repo):
        self.host_repo = str(host_repo)
        self.commands = []

    async def exec_tool(self, name, args, workspace=""):
        assert name == "bash", f"gateway must only ship bash, got {name}"
        self.commands.append(args["command"])
        proc = subprocess.run(
            args["command"], shell=True, cwd=self.host_repo,
            capture_output=True, text=True, timeout=30,
        )
        return {
            "exit_code": proc.returncode,
            "output": proc.stdout + proc.stderr,
            "timed_out": False,
            "truncated": False,
        }


class _OfflineSession:
    """A host that cannot be reached, the way RemoteSession.exec_tool
    surfaces a failed connection."""

    host_id = "h-offline"

    async def exec_tool(self, name, args, workspace=""):
        return {"error": "remote host unreachable: ConnectError: boom"}


@pytest.fixture()
def _clear_sessions():
    from backend.agent import remote as remote_mod

    remote_mod.clear_remote()
    yield
    remote_mod.clear_remote()


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
    # #361: the worktree hash trio is gone - one tree, one hash.
    assert "worktree_hash" not in info
    assert "worktree_ahead" not in info


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


def test_git_branch_reports_workspace_truth(client, tmp_path):
    """#361: the chip endpoint reads the ONE tree's checked-out branch - no
    per-chat pin, no origin, no staleness state."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    conv_id = _conversation(client, repo)
    assert client.get(f"/api/conversations/{conv_id}/git-branch").json() == {
        "branch": "master"
    }

    # The human (or the checkout action) moves the tree; the chip follows.
    _git(repo, "checkout", "feature")
    invalidate_git_caches(repo)
    assert client.get(f"/api/conversations/{conv_id}/git-branch").json() == {
        "branch": "feature"
    }


def test_workspace_git_branches_lists_the_destination(client, tmp_path):
    """Draft-card read: the destination workspace's branch list. The direct
    world carries no pin-at-creation - the chat follows the one tree."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")

    r = client.get("/api/workspaces/git-branches", params={"workspace": str(repo)})
    assert r.status_code == 200, r.text
    assert r.json() == {"branch": "master", "branches": ["feature", "master"]}


def test_workspace_git_endpoints_hide_nonrepo_default_and_remote(client, tmp_path):
    empty = tmp_path / "empty-workspace"
    empty.mkdir()
    assert client.get(
        "/api/workspaces/git-branches", params={"workspace": str(empty)}
    ).json() == {"branch": None, "branches": []}
    assert client.get(
        "/api/workspaces/git-branches", params={"workspace": ""}
    ).json() == {"branch": None, "branches": []}
    # #335: a remote destination is served through the gateway; with no
    # session for the host the answer is the explicit offline state -
    # the old silent-hide ({branch: None, branches: []}) is gone.
    assert client.get(
        "/api/workspaces/git-branches", params={"workspace": "remote:host"}
    ).json() == {"branch": None, "branches": [], "offline": True}


def test_git_command_checkout_moves_the_workspace_tree(client, tmp_path):
    """#361: the UI checkout action is a plain checkout of the ONE tree -
    git's own dirty-tree refusals are the guard."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    conv_id = _conversation(client, repo)
    r = client.post(
        f"/api/conversations/{conv_id}/git-command", json={"action": "checkout", "branch": "feature"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True

    # The tree moved.
    invalidate_git_caches(repo)
    info = client.get(f"/api/conversations/{conv_id}/git-info").json()["info"]
    assert info["branch"] == "feature"

    # The action still lands as a trace row (existing contract).
    msgs = client.get(f"/api/conversations/{conv_id}/messages").json()
    assert msgs[-1]["tool_call_id"].startswith("ui-git-checkout-")

    # Git's own refusal surfaces verbatim: a checkout that would clobber
    # uncommitted work fails with git's message, and nothing is stashed.
    _divergent_content(repo, "feature")
    (repo / "hello.txt").write_text("uncommitted master edit\n", encoding="utf-8")
    r2 = client.post(
        f"/api/conversations/{conv_id}/git-command", json={"action": "checkout", "branch": "feature"}
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["ok"] is False
    assert "local changes" in body2["error"] or "would be overwritten" in body2["error"]
    assert _git(repo, "branch", "--show-current") == "master"  # tree unmoved


def test_git_command_commit_stages_all_and_posts_trace_row(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    (repo / "hello.txt").write_text("hi\nmore\n", encoding="utf-8")
    (repo / "new.txt").write_text("n", encoding="utf-8")
    r = client.post(
        f"/api/conversations/{conv_id}/git-command", json={"action": "commit", "message": "wip"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    msgs = client.get(f"/api/conversations/{conv_id}/messages").json()
    row = msgs[-1]
    assert row["tool_call_id"].startswith("ui-git-commit-")
    assert "wip" in row["content"]


def test_git_command_commit_requires_message(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    r = client.post(
        f"/api/conversations/{conv_id}/git-command", json={"action": "commit", "message": "  "}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": False, "error": "commit message is empty"}


def test_git_command_status(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    (repo / "dirty.txt").write_text("x", encoding="utf-8")
    r = client.post(f"/api/conversations/{conv_id}/git-command", json={"action": "status"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "?? dirty.txt" in body["output"]


def test_git_command_push_without_upstream_falls_back(client, tmp_path):
    """Bare push on an upstream-less repo fails; the endpoint retries with
    --set-upstream and records the note (the push itself fails again here -
    origin points nowhere - but the fallback path is what's under test)."""
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


def test_git_command_push_follows_the_workspace_tree(client, tmp_path):
    """#361: the run/* push refusal is gone - branches are branches. Push
    acts on the workspace's checked-out branch, whatever it is named."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    repo = _repo_with_commit(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "checkout", "-q", "-b", "run/named-whatever")
    conv_id = _conversation(client, repo)
    r = client.post(f"/api/conversations/{conv_id}/git-command", json={"action": "push"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body


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
    error) - the UI hides the whole cluster for non-repos, so this path is
    defense-in-depth, not a UX surface."""
    empty = tmp_path / "empty"
    empty.mkdir()
    conv_id = _conversation(client, empty)
    r = client.post(f"/api/conversations/{conv_id}/git-command", json={"action": "status"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "not a git repository" in body["error"]


# ---- #361: branch-select is a plain checkout of the workspace ----


def test_branch_select_switches_the_workspace_tree(client, tmp_path):
    """The direct world: one tree, and the switch moves it. Response shape
    {ok, branch, created}; the tree really checks out."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    conv_id = _conversation(client, repo)

    r = client.post(f"/api/conversations/{conv_id}/branch-select", json={"branch": "feature"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"ok": True, "branch": "feature", "created": False}

    invalidate_git_caches(repo)
    info = client.get(f"/api/conversations/{conv_id}/git-info").json()["info"]
    assert info["branch"] == "feature"


def test_branch_select_creates_a_missing_branch(client, tmp_path):
    """create defaults true: an unknown name is created from HEAD and
    checked out."""
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)
    r = client.post(f"/api/conversations/{conv_id}/branch-select", json={"branch": "fresh"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "branch": "fresh", "created": True}
    assert _git(repo, "branch", "--show-current") == "fresh"


def test_branch_select_refusals_surface_verbatim(client, tmp_path):
    """Empty, option-like names are refused; git's own dirty-tree refusal
    arrives as the error text; nothing is stashed or discarded by the
    harness. Unknown conversations 404."""
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)

    for branch in ("", "--detach"):
        r = client.post(f"/api/conversations/{conv_id}/branch-select", json={"branch": branch})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert body.get("error")

    # Git's refusal: an uncommitted change to a file that DIFFERS between
    # the branches blocks the checkout (same content on both would carry
    # over harmlessly - nothing to clobber).
    _divergent_content(repo, "feature")
    (repo / "hello.txt").write_text("uncommitted master edit\n", encoding="utf-8")
    r = client.post(f"/api/conversations/{conv_id}/branch-select", json={"branch": "feature"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "local changes" in body["error"] or "would be overwritten" in body["error"]
    assert _git(repo, "branch", "--show-current") == "master"  # unmoved
    assert _git(repo, "stash", "list") == ""  # nothing stashed behind the user's back

    assert client.post(
        f"/api/conversations/{conv_id}/branch-select", json={"branch": None}
    ).status_code == 422  # branch is required

    assert client.post(
        "/api/conversations/999999/branch-select", json={"branch": "master"}
    ).status_code == 404


def test_branch_select_refuses_remote_and_blank_workspaces(client, tmp_path):
    """remote: namespaces and the blank pseudo-workspace have no local
    checkout to switch."""
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)

    r = client.post(
        f"/api/conversations/{conv_id}/branch-select", json={"branch": "master"}
    )
    assert r.status_code == 200

    # A remote conversation is refused with a clear message.
    rid = client.post(
        "/api/conversations", json={"workspace": "remote:host:C:/x"}
    ).json()["id"]
    r2 = client.post(f"/api/conversations/{rid}/branch-select", json={"branch": "master"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["ok"] is False
    assert "remote" in r2.json()["error"]


def test_git_branch_remote_reads_the_host_through_the_gateway(client, tmp_path, _clear_sessions):
    """#361 keeps #333's remote leg: a remote conversation's chip reads the
    HOST's checked-out branch through the gateway, never workspace_root
    (its Path.resolve would mangle the namespace into a client-local path).
    An unreachable host reports None - the offline posture, not a crash."""
    from backend.agent import remote as remote_mod
    from backend.agent.gitinfo import invalidate_git_caches

    host_repo = _repo_with_commit(tmp_path, name="host-repo")
    _git(host_repo, "checkout", "-q", "-b", "host-branch")

    session = _FakeHostSession(host_repo)
    remote_mod.register_remote(session)

    rid = client.post(
        "/api/conversations", json={"workspace": "remote:h-fake:C:/repo"}
    ).json()["id"]
    r = client.get(f"/api/conversations/{rid}/git-branch")
    assert r.status_code == 200, r.text
    assert r.json() == {"branch": "host-branch"}
    assert any("rev-parse" in c for c in session.commands), "must ship through the channel"

    # Offline host: explicit None, still a 200 - never a raise.
    remote_mod.register_remote(_OfflineSession())
    invalidate_git_caches("remote:h-offline:C:/repo")
    oid = client.post(
        "/api/conversations", json={"workspace": "remote:h-offline:C:/repo"}
    ).json()["id"]
    r2 = client.get(f"/api/conversations/{oid}/git-branch")
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"branch": None}


def test_branch_select_refuses_a_branch_held_by_a_registered_worktree(client, tmp_path):
    """The third verbatim refusal the spec names: the branch lives in a
    still-registered worktree, and git's own error is the whole guard."""
    main_repo = _repo_with_commit(tmp_path, name="main")
    linked = tmp_path / "linked"
    _git(main_repo, "worktree", "add", str(linked), "-b", "held")
    r = client.post(
        f"/api/conversations/{_conversation(client, main_repo)}/branch-select",
        json={"branch": "held"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "worktree" in body["error"] or "used by" in body["error"] or "checkout" in body["error"]
    assert _git(main_repo, "branch", "--show-current") == "master"
