"""Branch chip tri-state (issue #302, ADR-0010 amendment decision 1).

The chip distinguishes an inherited pin (the workspace's branch at
creation / no pick at all) from an explicit pick (draft card, selector,
or the agent's branch_select tool), and flags a stale pin whose branch
no longer exists locally. Chip-state persistence (branch_pin_origin)
lives here; pin-at-creation semantics stay in #301.

Scope: the selector endpoints' reporting plus the persistence the chip
needs. Touches nothing in the primary worktree.
"""
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.agent.gitinfo import invalidate_git_caches
from backend.db.database import (
    create_conversation,
    get_conversation,
    update_conversation,
)


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


@pytest.fixture()
def client():
    from backend.main import app

    with TestClient(app) as c:
        yield c


# ---- persistence: the pin's origin is chip state and survives ----


async def test_branch_pin_origin_column_exists():
    from backend.db.database import get_db

    db = await get_db()
    try:
        cur = await db.execute("PRAGMA table_info(conversations)")
        cols = {r[1] for r in await cur.fetchall()}
    finally:
        await db.close()
    assert "branch_pin_origin" in cols


@pytest.mark.asyncio
async def test_create_conversation_persists_explicit_origin():
    cid = await create_conversation("t", selected_branch="feature", branch_pin_origin="explicit")
    assert (await get_conversation(cid))["branch_pin_origin"] == "explicit"


@pytest.mark.asyncio
async def test_create_conversation_without_pick_has_null_origin():
    cid = await create_conversation("t")
    row = await get_conversation(cid)
    assert row["selected_branch"] is None
    assert row["branch_pin_origin"] is None


@pytest.mark.asyncio
async def test_origin_without_a_pin_is_not_stored():
    """An origin flag without a branch is meaningless — never stored."""
    cid = await create_conversation("t", branch_pin_origin="inherited")
    assert (await get_conversation(cid))["branch_pin_origin"] is None


@pytest.mark.asyncio
async def test_selector_pick_updates_origin_to_explicit():
    cid = await create_conversation("t", selected_branch="base", branch_pin_origin="inherited")
    await update_conversation(cid, selected_branch="next", branch_pin_origin="explicit")
    row = await get_conversation(cid)
    assert row["selected_branch"] == "next"
    assert row["branch_pin_origin"] == "explicit"


# ---- GET /git-branch: the chip's feed reports origin and staleness ----


@pytest.mark.asyncio
async def test_git_branch_reports_explicit_origin_for_legacy_pick(client, tmp_path):
    """A pre-#302 pick (origin NULL) was only ever a user pick: reported
    explicit, so legacy rows don't suddenly grow inherited markers."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    cid = await create_conversation("t", workspace=str(repo), selected_branch="feature")
    r = client.get(f"/api/conversations/{cid}/git-branch").json()
    assert r == {"branch": "feature", "pin_origin": "explicit", "stale": False}


@pytest.mark.asyncio
async def test_git_branch_reports_inherited_origin(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    cid = await create_conversation(
        "t", workspace=str(repo), selected_branch="master", branch_pin_origin="inherited"
    )
    r = client.get(f"/api/conversations/{cid}/git-branch").json()
    assert r["pin_origin"] == "inherited"
    assert r["stale"] is False


@pytest.mark.asyncio
async def test_git_branch_without_pick_reports_no_origin(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    cid = await create_conversation("t", workspace=str(repo))
    r = client.get(f"/api/conversations/{cid}/git-branch").json()
    # #301: a git-workspace chat is born pinned — the workspace's branch
    # at creation, stamped 'inherited' (the chip's inherited marker).
    assert r["branch"] == "master"
    assert r["pin_origin"] == "inherited"
    assert r["stale"] is False


@pytest.mark.asyncio
async def test_git_branch_flags_stale_pin_when_branch_deleted(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "doomed")
    cid = await create_conversation(
        "t", workspace=str(repo), selected_branch="doomed", branch_pin_origin="explicit"
    )
    _git(repo, "branch", "-D", "doomed")
    invalidate_git_caches(repo)
    r = client.get(f"/api/conversations/{cid}/git-branch").json()
    assert r["branch"] == "doomed"  # the dead name still surfaces...
    assert r["stale"] is True       # ...but flagged, not silently rendered


@pytest.mark.asyncio
async def test_git_branch_not_stale_when_branch_exists(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "alive")
    cid = await create_conversation(
        "t", workspace=str(repo), selected_branch="alive", branch_pin_origin="explicit"
    )
    invalidate_git_caches(repo)
    r = client.get(f"/api/conversations/{cid}/git-branch").json()
    assert r["stale"] is False


@pytest.mark.asyncio
async def test_git_branch_remote_workspace_never_stale(client):
    """remote:/no-workspace chats can't be checked against a local branch
    list — reported not-stale rather than guessed."""
    cid = await create_conversation("t", workspace="remote:box:/repo")
    await update_conversation(cid, selected_branch="feature")
    r = client.get(f"/api/conversations/{cid}/git-branch").json()
    assert r["stale"] is False
    assert r["branch"] == "feature"


# ---- POST /branch-select: a flip is an explicit pick ----


@pytest.mark.asyncio
async def test_branch_select_endpoint_stamps_explicit_origin(client, tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    cid = await create_conversation("t", workspace=str(repo))
    r = client.post(
        f"/api/conversations/{cid}/branch-select", json={"branch": "feature"}
    ).json()
    assert r["ok"] is True
    assert r["pin_origin"] == "explicit"
    row = await get_conversation(cid)
    assert row["selected_branch"] == "feature"
    assert row["branch_pin_origin"] == "explicit"


# ---- the agent-facing branch_select tool is an explicit pick too ----


@pytest.mark.asyncio
async def test_agent_branch_select_tool_stamps_explicit_origin(tmp_path):
    from backend.agent.tools import branch_select

    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    cid = await create_conversation("t")
    out = await branch_select(str(repo), branch="feature", conversation_id=cid)
    assert out["ok"] is True
    assert out["pin_origin"] == "explicit"
    row = await get_conversation(cid)
    assert row["branch_pin_origin"] == "explicit"


# ---- the run path: a stale pin reaches the model as a STALE note ----


async def _capture_system_prompt(cid, workspace):
    """Mirror of the #286 note test's harness: run one turn with the model
    client swapped for a capture stub."""
    import asyncio
    import gc
    from backend.agent import loop

    captured = {}

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        captured["system"] = messages[0]["content"]

        async def _stream():
            yield {"type": "content", "text": "done"}
            yield {"type": "finish"}

        return _stream()

    orig_chat = loop.model_client.chat
    orig_notes = loop._agents_notes
    loop.model_client.chat = fake_chat
    loop._agents_notes = lambda w: ""
    try:
        async for _ in loop.run_agent(cid, "go", str(workspace)):
            pass
    finally:
        loop.model_client.chat = orig_chat
        loop._agents_notes = orig_notes
        gc.collect()
    return captured["system"]


@pytest.mark.asyncio
async def test_run_reports_stale_pin_in_note(tmp_path):
    repo = _repo_with_commit(tmp_path)
    cid = await create_conversation(
        "t", workspace=str(repo), selected_branch="gone", branch_pin_origin="explicit"
    )
    system = await _capture_system_prompt(cid, repo)
    assert "# Branch selector: gone (STALE)" in system
    assert "no longer exists" in system
    # The stale note never claims isolation it did not get.
    assert "per-chat worktree" not in system


@pytest.mark.asyncio
async def test_run_live_pin_gets_standard_note_not_stale(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    cid = await create_conversation(
        "t", workspace=str(repo), selected_branch="feature", branch_pin_origin="explicit"
    )
    system = await _capture_system_prompt(cid, repo)
    assert "# Branch selector: feature" in system
    assert "(STALE)" not in system


@pytest.mark.asyncio
async def test_run_non_repo_workspace_never_flags_stale(tmp_path):
    """A non-repo workspace has no branch list — the guard must read that
    as 'cannot check', never as 'the branch was deleted'."""
    cid = await create_conversation(
        "t", workspace=str(tmp_path), selected_branch="bigtest", branch_pin_origin="explicit"
    )
    system = await _capture_system_prompt(cid, tmp_path)
    assert "(STALE)" not in system
