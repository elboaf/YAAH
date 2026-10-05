"""Scheduled-agent landing modes (issue #278, ADR-0010).

A scheduled fire has no user present to pick a branch, so the agent
carries its own landing setting:

- off (default) — today's behavior: the pinned chat runs on its own
  selected branch; landing follows the chat SOP (#277).
- fixed — every fire lands on landing_branch (written through to the
  chat's pin at fire time; the branch must exist locally).
- per-run — each fire gets its own branch <agent>-<YYYYMMDD-HHMM>
  (collision-bumped, never overwritten), derived from the chat's pin,
  left unmerged for manual integration: the harness never merges into
  shared trees unattended, and the primary worktree is never touched.

Landing itself stays prompt-driven: the fire writes the target through
to the conversation's selected_branch and the #277 run seam does the
rest (chat worktree + selector note).
"""
import asyncio
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.agent import loop
from backend.agent import scheduler as sched
from backend.agent.gitinfo import invalidate_git_caches
from backend.db.database import (
    create_agent,
    create_conversation,
    get_agent,
    get_conversation,
    update_agent,
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


def make_agent(workspace="C:/ws", name="nightly", prompt="summarize commits", **kw):
    """Row dict shaped like the API layer's create payload (mirrors
    test_scheduler's helper; landing fields ride on top)."""
    import json

    fields = {
        "workspace": workspace,
        "name": name,
        "prompt": prompt,
        "schedule_type": "interval",
        "schedule_spec": json.dumps({"minutes": 30}),
        "approval_policy": "sandbox-only",
        "enabled": 1,
        "memory_enabled": 1,
    }
    fields.update(kw)
    return fields


async def _agent_with_conv(workspace, **agent_kw):
    conv = await create_conversation("agent chat", workspace=workspace, chat_type="agent")
    row = await create_agent(make_agent(workspace=workspace, conversation_id=conv, **agent_kw))
    return conv, row


async def _fire_once(agent_row, captured):
    """Stub run_agent, fire once, wait for the fire task to settle."""

    async def fake_run(cid, prompt, workspace, **kw):
        captured.append({"cid": cid, "prompt": prompt, "workspace": workspace})
        return
        yield  # pragma: no cover — async generator formality

    original = loop.run_agent
    loop.run_agent = fake_run
    try:
        assert await sched.fire_agent(agent_row) == "started"
        for _ in range(300):
            await asyncio.sleep(0.01)
            fresh = await get_agent(agent_row["id"])
            if fresh["last_status"] in ("ok", "error"):
                break
    finally:
        loop.run_agent = original
    fresh = await get_agent(agent_row["id"])
    assert fresh["last_status"] == "ok", fresh["last_status"]
    return captured


# ---- resolve_landing: normalization + the fixed-mode existence check ----


@pytest.mark.asyncio
async def test_off_is_the_default_and_junk_normalizes_to_off():
    conv, row = await _agent_with_conv("C:/ws")
    assert await sched.resolve_landing(row["id"]) == ("off", "")
    await update_agent(row["id"], {"landing_mode": "bogus"})
    assert await sched.resolve_landing(row["id"]) == ("off", "")
    await update_agent(row["id"], {"landing_mode": "fixed", "landing_branch": ""})
    # fixed with no branch is meaningless — reads as off, never as a fire
    # against an empty name.
    assert await sched.resolve_landing(row["id"]) == ("off", "")


@pytest.mark.asyncio
async def test_fixed_mode_requires_the_branch_to_exist(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "nightly")
    conv, row = await _agent_with_conv(
        str(repo), landing_mode="fixed", landing_branch="nightly"
    )
    assert await sched.resolve_landing(row["id"]) == ("fixed", "nightly")
    _git(repo, "branch", "-D", "nightly")
    invalidate_git_caches(repo)
    # A dead fixed target would hand the run a stale pin (#302) — the
    # fire keeps the chat's own branch instead.
    assert await sched.resolve_landing(row["id"]) == ("off", "")


@pytest.mark.asyncio
async def test_fixed_mode_on_remote_workspace_reads_as_off():
    # Remote workspaces are out of scope v1: no branch list to check.
    conv, row = await _agent_with_conv(
        "remote:box:/repo", landing_mode="fixed", landing_branch="nightly"
    )
    assert await sched.resolve_landing(row["id"]) == ("off", "")


# ---- the fire path: write-through to the chat's pin ----


@pytest.mark.asyncio
async def test_fixed_fire_writes_branch_through_to_chat_pin(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "nightly")
    conv, row = await _agent_with_conv(
        str(repo), landing_mode="fixed", landing_branch="nightly"
    )
    captured = await _fire_once(row, [])
    assert captured[0]["cid"] == conv
    pin = await get_conversation(conv)
    assert pin["selected_branch"] == "nightly"
    assert pin["branch_pin_origin"] == "explicit"
    # off-behavior guard: no per-run branch was created.
    branches = _git(repo, "branch", "--list", "nightly*")
    assert branches.strip() == "* nightly" or branches.strip() == "nightly"


@pytest.mark.asyncio
async def test_per_run_fire_creates_branch_writes_pin_and_landing_note(tmp_path):
    repo = _repo_with_commit(tmp_path)
    conv, row = await _agent_with_conv(str(repo), landing_mode="per-run")
    captured = await _fire_once(row, [])
    pin = await get_conversation(conv)
    name = pin["selected_branch"]
    assert re.fullmatch(r"[a-z0-9-]+-\d{8}-\d{4}", name), name
    assert pin["branch_pin_origin"] == "explicit"
    # The branch exists and is checked out NOWHERE (creation only — the
    # chat worktree materializes inside the run, which the stub skips).
    assert name in _git(repo, "branch", "--list", name)
    # The fire's prompt carries the leave-it-unmerged landing rule.
    assert "# Landing" in captured[0]["prompt"]
    assert "never" in captured[0]["prompt"]


@pytest.mark.asyncio
async def test_off_fire_leaves_chat_pin_and_prompt_alone(tmp_path):
    conv, row = await _agent_with_conv("C:/ws")
    before = await get_conversation(conv)
    captured = await _fire_once(row, [])
    after = await get_conversation(conv)
    assert before["selected_branch"] == after["selected_branch"]
    assert before["branch_pin_origin"] == after["branch_pin_origin"]
    assert "# Landing" not in captured[0]["prompt"]


# ---- per-run branch creation: naming, collisions, start point ----


@pytest.mark.asyncio
async def test_per_run_branch_never_collides_or_overwrites(tmp_path):
    repo = _repo_with_commit(tmp_path)
    first = await sched._create_per_run_branch(str(repo), "aB12-cD34")
    assert first is not None and first.startswith("ab12-cd34-")
    # Same minute, same agent: the second fire bumps instead of clobbering.
    second = await sched._create_per_run_branch(str(repo), "aB12-cD34")
    assert second != first
    assert second == f"{first}-2"
    assert first in _git(repo, "branch", "--list", first)
    assert second in _git(repo, "branch", "--list", second)


@pytest.mark.asyncio
async def test_per_run_branch_derives_from_the_chat_pin_not_primary_head(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "base")
    # Move master ahead so the start point is actually distinguishable
    # from the primary's HEAD (a one-commit repo pins them together).
    (repo / "second.txt").write_text("2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    conv = await create_conversation("t", workspace=str(repo), chat_type="agent")
    await update_conversation(conv, selected_branch="base")
    name = await sched._create_per_run_branch(str(repo), "agent-x", start_point="base")
    assert name is not None
    # The new branch points at the chat pin's tip, not master's.
    assert _git(repo, "rev-parse", name) == _git(repo, "rev-parse", "base")
    assert _git(repo, "rev-parse", name) != _git(repo, "rev-parse", "master")


@pytest.mark.asyncio
async def test_per_run_creation_on_non_repo_is_none():
    assert await sched._create_per_run_branch("C:/definitely-not-a-repo", "x") is None


# ---- API surface: validation + the editor round-trip ----


def _client():
    from backend.main import app

    return TestClient(app)


def test_api_validates_landing_mode_and_fixed_branch():
    with _client() as c:
        base = {
            "name": "landing test",
            "prompt": "p",
            "schedule_type": "interval",
            "schedule_spec": {"minutes": 30},
        }
        r = c.post("/api/agents", json={**base, "landing_mode": "sometimes"})
        assert r.status_code == 400
        r = c.post("/api/agents", json={**base, "landing_mode": "fixed"})
        assert r.status_code == 400  # fixed without a branch
        r = c.post(
            "/api/agents",
            json={**base, "landing_mode": "fixed", "landing_branch": "nightly"},
        )
        assert r.status_code == 200
        assert r.json()["landing_mode"] == "fixed"
        assert r.json()["landing_branch"] == "nightly"
        # Non-fixed modes never carry a stale branch name.
        r = c.post("/api/agents", json={**base, "landing_mode": "per-run",
                                        "landing_branch": "ghost"})
        assert r.status_code == 200
        assert r.json()["landing_branch"] == ""


def test_api_patch_updates_landing_settings():
    with _client() as c:
        base = {
            "name": "landing patch",
            "prompt": "p",
            "schedule_type": "interval",
            "schedule_spec": {"minutes": 30},
        }
        created = c.post("/api/agents", json=base).json()
        aid = created["id"]
        r = c.patch(
            f"/api/agents/{aid}",
            json={**base, "landing_mode": "per-run", "landing_branch": ""},
        )
        assert r.status_code == 200
        assert r.json()["landing_mode"] == "per-run"
        r = c.patch(
            f"/api/agents/{aid}",
            json={**base, "landing_mode": "fixed", "landing_branch": "b"},
        )
        assert r.status_code == 200
        assert r.json()["landing_branch"] == "b"
        r = c.patch(
            f"/api/agents/{aid}",
            json={**base, "landing_mode": "fixed", "landing_branch": ""},
        )
        assert r.status_code == 400


def test_agent_editor_round_trip_preserves_landing():
    """agentToBody must carry the landing fields or a pause/resume would
    silently reset them to off."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "src" / "components.tsx").read_text(encoding="utf-8")
    assert "landing_mode: a.landing_mode" in source
    assert "landing_branch: a.landing_branch" in source
