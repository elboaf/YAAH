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


def test_workspace_git_branches_and_pin_at_creation(client, tmp_path):
    """#301: with the draft-card endpoint gone, "switch to X" before a chat
    exists is a selector write. A no-pick creation pins the workspace's
    branch as 'inherited', and a terminal `git switch` afterwards changes
    nothing the chat aims at (semantic primary-tree immunity)."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")

    r = client.get("/api/workspaces/git-branches", params={"workspace": str(repo)})
    assert r.status_code == 200, r.text
    assert r.json() == {"branch": "master", "branches": ["feature", "master"]}

    conv_id = _conversation(client, repo)
    g = client.get(f"/api/conversations/{conv_id}/git-branch").json()
    assert g == {"branch": "master", "pin_origin": "inherited", "stale": False}


def test_workspace_git_checkout_endpoint_is_gone(client, tmp_path):
    """#301: no in-YAAH control moves the primary worktree anymore."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    from backend.main import app

    assert all(
        getattr(rt, "path", "") != "/api/workspaces/git-checkout"
        for rt in app.routes
    )
    r = client.post(
        "/api/workspaces/git-checkout",
        json={"workspace": str(repo), "branch": "feature"},
    )
    assert r.status_code in (404, 405)  # no handler either way
    assert _git(repo, "branch", "--show-current") == "master"


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


def test_git_command_checkout_writes_selector_not_tree(client, tmp_path):
    """#286: the UI checkout action records the chat's intended branch; the
    shared workspace tree stays put (no other chat may be standing on it)."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    conv_id = _conversation(client, repo)
    r = client.post(
        f"/api/conversations/{conv_id}/git-command", json={"action": "checkout", "branch": "feature"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True

    # The tree did not move.
    invalidate_git_caches(repo)
    info = client.get(f"/api/conversations/{conv_id}/git-info").json()["info"]
    assert info["branch"] == "master"

    # The chat's stored selection did.
    assert client.get(f"/api/conversations/{conv_id}").json()["selected_branch"] == "feature"

    # The action still lands as a trace row (existing contract).
    msgs = client.get(f"/api/conversations/{conv_id}/messages").json()
    assert msgs[-1]["tool_call_id"].startswith("ui-git-checkout-")


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


# ---- #286: the branch selector becomes a per-chat stored branch value ----


def test_branch_select_updates_stored_value_without_tree_move(client, tmp_path):
    """The core slice: flipping the selector runs no git checkout — the
    shared tree stays where it is, and the pick is stored on the chat row."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    conv_id = _conversation(client, repo)

    r = client.post(f"/api/conversations/{conv_id}/branch-select", json={"branch": "feature"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "selected_branch": "feature", "pin_origin": "explicit"}

    # The stored value updated…
    assert client.get(f"/api/conversations/{conv_id}").json()["selected_branch"] == "feature"

    # …and the physical tree did not move.
    invalidate_git_caches(repo)
    info = client.get(f"/api/conversations/{conv_id}/git-info").json()["info"]
    assert info["branch"] == "master"
    assert info["dirty"] is False


def test_branch_select_flips_are_private_per_chat(client, tmp_path):
    """Two chats on one workspace flip selectors independently; neither
    moves the shared tree the other chat reads."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    conv_a = _conversation(client, repo)
    conv_b = _conversation(client, repo)

    assert client.post(
        f"/api/conversations/{conv_a}/branch-select", json={"branch": "feature"}
    ).json()["ok"] is True
    assert client.post(
        f"/api/conversations/{conv_b}/branch-select", json={"branch": "master"}
    ).json()["ok"] is True

    # Each chat reads its own pick (#302: explicit origin, not stale)…
    assert client.get(f"/api/conversations/{conv_a}/git-branch").json() == {
        "branch": "feature", "pin_origin": "explicit", "stale": False,
    }
    assert client.get(f"/api/conversations/{conv_b}/git-branch").json() == {
        "branch": "master", "pin_origin": "explicit", "stale": False,
    }

    # …and the single physical tree is untouched throughout.
    invalidate_git_caches(repo)
    assert _git(repo, "branch", "--show-current") == "master"


def test_branch_select_validates_like_checkout(client, tmp_path):
    """Empty, option-like, and non-local names are refused; unknown
    conversations 404."""
    repo = _repo_with_commit(tmp_path)
    conv_id = _conversation(client, repo)

    for branch in ("", "--detach"):
        r = client.post(f"/api/conversations/{conv_id}/branch-select", json={"branch": branch})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert body.get("error")

    r = client.post(f"/api/conversations/{conv_id}/branch-select", json={"branch": "no-such-branch"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": False, "error": "not a local branch: no-such-branch"}

    assert client.post(
        f"/api/conversations/{conv_id}/branch-select", json={"branch": None}
    ).status_code == 422  # branch is required

    assert client.post(
        "/api/conversations/999999/branch-select", json={"branch": "master"}
    ).status_code == 404

    # Nothing was written by the refused calls: the chat keeps the pin it
    # was born with (#301: creation already stamped the workspace branch).
    conv = client.get(f"/api/conversations/{conv_id}").json()
    assert conv["selected_branch"] == "master"
    assert conv["branch_pin_origin"] == "inherited"


def test_chat_pinned_at_creation_does_not_follow_the_tree(client, tmp_path):
    """#301 supersedes the pre-#301 fallback: a git-workspace chat is born
    pinned to the workspace's then-current branch ('inherited'), so a
    terminal `git switch` on the primary tree afterwards changes nothing
    the chat aims at (semantic primary-tree immunity). An explicit pick
    still overrides, and git-info keeps reporting the physical tree."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    conv_id = _conversation(client, repo)

    # Born pinned: the workspace's branch at creation, marked inherited.
    assert client.get(f"/api/conversations/{conv_id}/git-branch").json() == {
        "branch": "master", "pin_origin": "inherited", "stale": False,
    }

    # The human checks out feature in the shared tree (their tool); the
    # chat's inherited pin does not follow along.
    _git(repo, "checkout", "feature")
    invalidate_git_caches(repo)
    assert client.get(f"/api/conversations/{conv_id}/git-branch").json() == {
        "branch": "master", "pin_origin": "inherited", "stale": False,
    }

    # The chat flips: its explicit pick now wins over everything…
    assert client.post(
        f"/api/conversations/{conv_id}/branch-select", json={"branch": "master"}
    ).json()["ok"] is True
    assert client.get(f"/api/conversations/{conv_id}/git-branch").json() == {
        "branch": "master", "pin_origin": "explicit", "stale": False,
    }

    # …while git-info keeps reporting the physical tree.
    invalidate_git_caches(repo)
    assert client.get(f"/api/conversations/{conv_id}/git-info").json()["info"]["branch"] == "feature"


async def test_no_path_moves_the_primary_tree(client, tmp_path):
    """#301: the selector flip records intent and moves nothing — the tree
    is the human's. The endpoint that used to move it is gone entirely."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    conv_id = _conversation(client, repo)
    assert client.post(
        f"/api/conversations/{conv_id}/branch-select", json={"branch": "feature"}
    ).json()["ok"] is True

    assert _git(repo, "branch", "--show-current") == "master"  # the tree stayed


async def test_selected_branch_migration_on_legacy_db(monkeypatch, tmp_path):
    """A pre-#286 database (conversations without selected_branch) upgrades
    in place; existing rows read NULL and fall back to the workspace branch."""
    import aiosqlite

    import backend.db.database as database

    old_db = tmp_path / "legacy286.db"
    async with aiosqlite.connect(old_db) as db:
        await db.execute(
            """CREATE TABLE conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT 'New Task',
                workspace TEXT,
                system_prompt_override TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        await db.execute(
            "INSERT INTO conversations (title, workspace) VALUES ('old', '/somewhere')"
        )
        await db.commit()

    monkeypatch.setattr(database, "DB_PATH", old_db)
    await database.init_db()
    db = await database.get_db()
    try:
        cur = await db.execute("PRAGMA table_info(conversations)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "selected_branch" in cols
        cur = await db.execute("SELECT title, selected_branch FROM conversations")
        row = await cur.fetchone()
        assert row["title"] == "old"
        assert row["selected_branch"] is None
    finally:
        await db.close()
