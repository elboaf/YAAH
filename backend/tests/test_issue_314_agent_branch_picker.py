"""Issue #314: the agent form gets a branch picker (ADR-0010).

`POST /api/agents` never passed a branch to `create_conversation`, so an
agent's pinned chat always took the #301 fallback: the workspace's
then-current branch, stamped `inherited` — a branch the user never chose
and could not see at creation. The brief (triage, 2026-10-05) specifies:

- Part A — an optional branch on the agent API: an explicit pick pins the
  new chat (origin `explicit`); absent/blank keeps the #301 fallback
  byte-for-byte (backward compatible); the edit PATCH writes an explicit
  pick through to the pinned chat's selector and leaves the chat alone
  on the inherit default.
- Part B — the editor surfaces the effective landing target: with
  landing `off`, `_agent_view` carries the pinned chat's branch and pin
  origin so the form can name what its fires would land on.
"""
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.agent.gitinfo import invalidate_git_caches
from backend.db.database import get_conversation


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _repo(tmp_path, name="repo", branch="master"):
    """A git repo with one commit, on the named branch."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


def _bare_dir(tmp_path, name="not-a-repo"):
    d = tmp_path / name
    d.mkdir()
    (d / "readme.txt").write_text("x\n", encoding="utf-8")
    return d


def _client():
    from backend.main import app

    return TestClient(app)


def _body(**kw):
    base = {
        "name": "picker test",
        "prompt": "p",
        "schedule_type": "interval",
        "schedule_spec": {"minutes": 30},
    }
    base.update(kw)
    return base


async def _chat(cid):
    return await get_conversation(cid)


# ---- Part A: creation ----


def test_create_with_explicit_branch_pins_the_new_chat(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "branch", "nightly")
    invalidate_git_caches(str(repo))
    with _client() as c:
        r = c.post(
            "/api/agents",
            json=_body(workspace=str(repo), selected_branch="nightly"),
        )
        assert r.status_code == 200
        conv = r.json()["conversation_id"]
        chat = _chat_sync(conv)
        assert chat["selected_branch"] == "nightly"
        assert chat["branch_pin_origin"] == "explicit"


def _chat_sync(cid):
    """get_conversation is async; the TestClient loop is not the test's."""
    import asyncio

    return asyncio.run(get_conversation(cid))


def test_create_without_a_branch_keeps_the_inherit_fallback(tmp_path):
    repo = _repo(tmp_path)
    invalidate_git_caches(str(repo))
    with _client() as c:
        r = c.post("/api/agents", json=_body(workspace=str(repo)))
        assert r.status_code == 200
        chat = _chat_sync(r.json()["conversation_id"])
        assert chat["selected_branch"] == "master"
        assert chat["branch_pin_origin"] == "inherited"


def test_create_body_without_the_field_still_works(tmp_path):
    """Backward compatibility: an older client omitting selected_branch
    entirely creates the agent exactly as before."""
    repo = _repo(tmp_path)
    invalidate_git_caches(str(repo))
    with _client() as c:
        body = _body(workspace=str(repo))
        body.pop("selected_branch", None)
        r = c.post("/api/agents", json=body)
        assert r.status_code == 200
        assert r.json()["id"]


def test_create_blank_branch_means_inherit(tmp_path):
    repo = _repo(tmp_path)
    invalidate_git_caches(str(repo))
    with _client() as c:
        r = c.post(
            "/api/agents",
            json=_body(workspace=str(repo), selected_branch=""),
        )
        assert r.status_code == 200
        chat = _chat_sync(r.json()["conversation_id"])
        assert chat["selected_branch"] == "master"
        assert chat["branch_pin_origin"] == "inherited"


def test_create_explicit_pick_wins_over_the_workspace_branch(tmp_path):
    repo = _repo(tmp_path, branch="master")
    _git(repo, "branch", "elsewhere")
    invalidate_git_caches(str(repo))
    with _client() as c:
        r = c.post(
            "/api/agents",
            json=_body(workspace=str(repo), selected_branch="elsewhere"),
        )
        assert r.status_code == 200
        chat = _chat_sync(r.json()["conversation_id"])
        # Not the workspace's then-current branch — the user's pick.
        assert chat["selected_branch"] == "elsewhere"
        assert chat["branch_pin_origin"] == "explicit"


# ---- Part A: the edit path ----


def test_patch_explicit_branch_writes_through_to_the_pinned_chat(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "branch", "nightly")
    invalidate_git_caches(str(repo))
    with _client() as c:
        created = c.post("/api/agents", json=_body(workspace=str(repo))).json()
        aid = created["id"]
        conv = created["conversation_id"]
        assert _chat_sync(conv)["selected_branch"] == "master"
        r = c.patch(
            f"/api/agents/{aid}",
            json=_body(workspace=str(repo), selected_branch="nightly"),
        )
        assert r.status_code == 200
        chat = _chat_sync(conv)
        assert chat["selected_branch"] == "nightly"
        assert chat["branch_pin_origin"] == "explicit"


def test_patch_on_the_inherit_default_leaves_the_chat_alone(tmp_path):
    """fixed/per-run overwrite the selector at every fire anyway — the
    edit control matters in off mode, so the default must be a no-op,
    not a silent reset to the workspace's branch."""
    repo = _repo(tmp_path)
    _git(repo, "branch", "nightly")
    invalidate_git_caches(str(repo))
    with _client() as c:
        created = c.post(
            "/api/agents",
            json=_body(workspace=str(repo), selected_branch="nightly"),
        ).json()
        aid = created["id"]
        conv = created["conversation_id"]
        assert _chat_sync(conv)["selected_branch"] == "nightly"
        r = c.patch(f"/api/agents/{aid}", json=_body(workspace=str(repo)))
        assert r.status_code == 200
        chat = _chat_sync(conv)
        assert chat["selected_branch"] == "nightly"
        assert chat["branch_pin_origin"] == "explicit"


# ---- Part B: the editor can name the target ----


def test_agent_view_surfaces_the_chat_pin(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "branch", "nightly")
    invalidate_git_caches(str(repo))
    with _client() as c:
        created = c.post(
            "/api/agents",
            json=_body(workspace=str(repo), selected_branch="nightly"),
        ).json()
        assert created["chat_selected_branch"] == "nightly"
        assert created["chat_branch_pin_origin"] == "explicit"
        # And after the inherit path:
        created = c.post("/api/agents", json=_body(workspace=str(repo))).json()
        assert created["chat_selected_branch"] == "master"
        assert created["chat_branch_pin_origin"] == "inherited"


def test_agent_view_pin_fields_are_null_without_a_pin(tmp_path):
    """Non-git workspace: no pin exists; the form shows 'no pin'."""
    d = _bare_dir(tmp_path)
    with _client() as c:
        created = c.post("/api/agents", json=_body(workspace=str(d))).json()
        assert created["chat_selected_branch"] is None
        assert created["chat_branch_pin_origin"] is None
