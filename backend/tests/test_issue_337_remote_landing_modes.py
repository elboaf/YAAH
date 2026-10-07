"""Scheduled landing modes live for remote chats (issue #337, spec #332).

The #278 landing resolution stops degrading remote workspaces to 'off':
the scheduler resolves through the #333 gateway exactly as it does
locally — fixed validates its branch against the host's branches, per-run
creates its collision-bumped branch on the host — and the write-through
to the chat's pin proceeds exactly as local. This ends the silent "off"
degradation. Flagged behavior change per the spec: dormant fixed/per-run
settings on existing remote agents begin steering their runs at the
next fire.

The universal degradation is host-offline at fire: the fire logs and
keeps the chat's pin — the run still lands inside the chat's worktree
per the run SOP, never nowhere in particular.

Tests inject a fake channel session (no real HTTP; the _StubSession
pattern from test_remote.py / test_issue_333).
"""
import asyncio
import re
import subprocess

import pytest

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


class _OfflineSession:
    """A host that cannot be reached: exec_tool returns the error dict,
    exactly as RemoteSession.exec_tool does on connection failure."""

    host_id = "h-offline"

    async def exec_tool(self, name, args, workspace=""):
        return {"error": "remote host unreachable: ConnectError: boom"}


class _FakeHostSession:
    """A host whose repository is a real local repo the test prepared;
    git commands ship through exec_tool and run against it (the wire
    shape is run_bash's: {exit_code, output, ...} or {error})."""

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


@pytest.fixture()
def _clear_sessions():
    from backend.agent import remote as remote_mod

    remote_mod.clear_remote()
    yield
    remote_mod.clear_remote()


def make_agent(workspace="C:/ws", name="nightly", prompt="summarize commits", **kw):
    """Row dict shaped like the API layer's create payload (mirrors
    test_issue_278's helper)."""
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


# ---- fixed mode: resolution through the gateway ----


@pytest.mark.asyncio
async def test_remote_fixed_mode_resolution_follows_the_host_branches(
    tmp_path, _clear_sessions
):
    """Fixed mode asks the HOST repo for its branch list: a live branch
    resolves, a missing one keeps the chat pin — the same #302 rule the
    local path has, just over the wire. Never the old silent remote
    degradation: the answer is data from the host, not a hardcoded off."""
    from backend.agent.remote import ns_path, register_remote

    repo = _repo_with_commit(tmp_path, "fixedrepo")
    register_remote(_FakeHostSession(repo))
    ws = ns_path("h-fake", str(repo))
    conv, row = await _agent_with_conv(
        ws, landing_mode="fixed", landing_branch="nightly"
    )
    # The branch does not exist on the host yet: the miss keeps the pin.
    assert await sched.resolve_landing(row["id"]) == ("off", "")
    _git(repo, "branch", "nightly")
    invalidate_git_caches(ws)  # branch changed under the app, as the local test does
    # Now it does: the fire steers.
    assert await sched.resolve_landing(row["id"]) == ("fixed", "nightly")


@pytest.mark.asyncio
async def test_remote_fixed_mode_host_offline_degrades_loudly(
    tmp_path, _clear_sessions, caplog
):
    """Host unreachable at resolution: the fire cannot check the host's
    branches, so it keeps the chat pin — but it SAYS so (the offline arm
    is distinguishable from a live host's missing-branch miss, which has
    its own log)."""
    import logging

    from backend.agent.remote import ns_path, register_remote

    register_remote(_OfflineSession())
    ws = ns_path("h-offline", "C:/repo")
    conv, row = await _agent_with_conv(
        ws, landing_mode="fixed", landing_branch="nightly"
    )
    with caplog.at_level(logging.WARNING, logger="yaah.scheduler"):
        assert await sched.resolve_landing(row["id"]) == ("off", "")
    assert "host unreachable" in caplog.text


@pytest.mark.asyncio
async def test_remote_fixed_fire_writes_pin_through(tmp_path, _clear_sessions):
    from backend.agent.remote import ns_path, register_remote

    repo = _repo_with_commit(tmp_path, "fixedfire")
    _git(repo, "branch", "nightly")
    register_remote(_FakeHostSession(repo))
    ws = ns_path("h-fake", str(repo))
    conv, row = await _agent_with_conv(
        ws, landing_mode="fixed", landing_branch="nightly"
    )
    captured = await _fire_once(row, [])
    assert captured[0]["cid"] == conv
    pin = await get_conversation(conv)
    assert pin["selected_branch"] == "nightly"
    assert pin["branch_pin_origin"] == "explicit"
    # off-behavior guard: no per-run branch was created on the host.
    assert _git(repo, "branch", "--list", "*-*").strip() == ""


@pytest.mark.asyncio
async def test_remote_fire_host_offline_keeps_pin_and_logs(
    tmp_path, _clear_sessions, caplog
):
    """Host offline at fire: explicit degradation to off with a log; the
    run keeps the chat's pin (and the run SOP still lands it inside the
    chat's worktree — the offline posture, never nowhere-in-particular)."""
    import logging

    from backend.agent.remote import ns_path, register_remote

    register_remote(_OfflineSession())
    ws = ns_path("h-offline", "C:/repo")
    conv, row = await _agent_with_conv(
        ws, landing_mode="fixed", landing_branch="nightly"
    )
    before = await get_conversation(conv)
    with caplog.at_level(logging.WARNING, logger="yaah.scheduler"):
        captured = await _fire_once(row, [])
    after = await get_conversation(conv)
    assert before["selected_branch"] == after["selected_branch"]
    assert before["branch_pin_origin"] == after["branch_pin_origin"]
    assert "host unreachable" in caplog.text
    assert captured[0]["cid"] == conv


# ---- per-run mode: branch creation on the host ----


@pytest.mark.asyncio
async def test_remote_per_run_fire_creates_branch_on_host_and_writes_pin(
    tmp_path, _clear_sessions
):
    from backend.agent.remote import ns_path, register_remote

    repo = _repo_with_commit(tmp_path, "perrunfire")
    register_remote(_FakeHostSession(repo))
    ws = ns_path("h-fake", str(repo))
    conv, row = await _agent_with_conv(ws, landing_mode="per-run")
    captured = await _fire_once(row, [])
    pin = await get_conversation(conv)
    name = pin["selected_branch"]
    assert re.fullmatch(r"[a-z0-9-]+-\d{8}-\d{4}", name), name
    assert pin["branch_pin_origin"] == "explicit"
    # The branch exists ON THE HOST and is checked out nowhere (creation
    # only — the chat worktree materializes inside the run).
    assert name in _git(repo, "branch", "--list", name)
    # The fire's prompt carries the leave-it-unmerged landing rule.
    assert "# Landing" in captured[0]["prompt"]
    assert "never" in captured[0]["prompt"]


@pytest.mark.asyncio
async def test_remote_per_run_branch_derives_from_the_chat_pin(
    tmp_path, _clear_sessions
):
    """ADR-0010: new branches derive from the chat's selection, never the
    primary's HEAD — on the host exactly as locally."""
    from backend.agent.remote import ns_path, register_remote

    repo = _repo_with_commit(tmp_path, "perrunbase")
    _git(repo, "branch", "base")
    (repo / "second.txt").write_text("2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    register_remote(_FakeHostSession(repo))
    ws = ns_path("h-fake", str(repo))
    conv = await create_conversation("t", workspace=ws, chat_type="agent")
    await update_conversation(conv, selected_branch="base")
    name = await sched._create_per_run_branch(ws, "agent-x", start_point="base")
    assert name is not None
    assert _git(repo, "rev-parse", name) == _git(repo, "rev-parse", "base")
    assert _git(repo, "rev-parse", name) != _git(repo, "rev-parse", "master")


@pytest.mark.asyncio
async def test_remote_per_run_branch_never_collides_or_overwrites(
    tmp_path, _clear_sessions
):
    from backend.agent.remote import ns_path, register_remote

    repo = _repo_with_commit(tmp_path, "perruncol")
    register_remote(_FakeHostSession(repo))
    ws = ns_path("h-fake", str(repo))
    first = await sched._create_per_run_branch(ws, "aB12-cD34")
    assert first is not None and first.startswith("ab12-cd34-")
    # Same minute, same agent: the second fire bumps instead of clobbering.
    second = await sched._create_per_run_branch(ws, "aB12-cD34")
    assert second != first
    assert second == f"{first}-2"
    assert first in _git(repo, "branch", "--list", first)
    assert second in _git(repo, "branch", "--list", second)


@pytest.mark.asyncio
async def test_remote_per_run_creation_host_offline_is_none(
    _clear_sessions, caplog
):
    """Creation failure keeps the chat's current pin — and the offline arm
    says why it failed instead of returning None silently."""
    import logging

    from backend.agent.remote import ns_path, register_remote

    register_remote(_OfflineSession())
    ws = ns_path("h-offline", "C:/repo")
    with caplog.at_level(logging.WARNING, logger="yaah.scheduler"):
        assert await sched._create_per_run_branch(ws, "x") is None
    assert "host unreachable" in caplog.text


@pytest.mark.asyncio
async def test_remote_per_run_creation_non_repo_is_none(tmp_path, _clear_sessions):
    """A host dir that is not a repo refuses creation — and without
    shipping any branch command at all."""
    from backend.agent.remote import ns_path, register_remote

    bare = tmp_path / "notarepo"
    bare.mkdir()
    session = _FakeHostSession(bare)
    register_remote(session)
    ws = ns_path("h-fake", str(bare))
    assert await sched._create_per_run_branch(ws, "x") is None
    assert not any(c.startswith("git branch") for c in session.commands)


@pytest.mark.asyncio
async def test_remote_per_run_fire_host_offline_keeps_pin(
    tmp_path, _clear_sessions, caplog
):
    """Per-run creation failure (host offline) keeps the chat's current
    pin — the fire still runs, it just does not steer."""
    import logging

    from backend.agent.remote import ns_path, register_remote

    register_remote(_OfflineSession())
    ws = ns_path("h-offline", "C:/repo")
    conv, row = await _agent_with_conv(ws, landing_mode="per-run")
    before = await get_conversation(conv)
    with caplog.at_level(logging.WARNING, logger="yaah.scheduler"):
        captured = await _fire_once(row, [])
    after = await get_conversation(conv)
    assert before["selected_branch"] == after["selected_branch"]
    assert before["branch_pin_origin"] == after["branch_pin_origin"]
    assert "host unreachable" in caplog.text
    assert captured[0]["cid"] == conv


# ---- parity guards: what must not change ----


@pytest.mark.asyncio
async def test_remote_off_fire_leaves_pin_and_prompt_alone(tmp_path, _clear_sessions):
    from backend.agent.remote import ns_path, register_remote

    repo = _repo_with_commit(tmp_path, "offfire")
    register_remote(_FakeHostSession(repo))
    ws = ns_path("h-fake", str(repo))
    conv, row = await _agent_with_conv(ws)
    before = await get_conversation(conv)
    captured = await _fire_once(row, [])
    after = await get_conversation(conv)
    assert before["selected_branch"] == after["selected_branch"]
    assert before["branch_pin_origin"] == after["branch_pin_origin"]
    assert "# Landing" not in captured[0]["prompt"]


@pytest.mark.asyncio
async def test_local_fixed_mode_resolution_unchanged(tmp_path):
    """The local arm keeps its subprocess path and its answers (the
    gateway reroute must not disturb it)."""
    repo = _repo_with_commit(tmp_path, "localfixed")
    _git(repo, "branch", "nightly")
    conv, row = await _agent_with_conv(
        str(repo), landing_mode="fixed", landing_branch="nightly"
    )
    assert await sched.resolve_landing(row["id"]) == ("fixed", "nightly")
    _git(repo, "branch", "-D", "nightly")
    invalidate_git_caches(repo)
    assert await sched.resolve_landing(row["id"]) == ("off", "")


@pytest.mark.asyncio
async def test_local_per_run_branch_creation_unchanged(tmp_path):
    repo = _repo_with_commit(tmp_path, "localperrun")
    first = await sched._create_per_run_branch(str(repo), "aB12-cD34")
    assert first is not None and first.startswith("ab12-cd34-")
    second = await sched._create_per_run_branch(str(repo), "aB12-cD34")
    assert second == f"{first}-2"
    assert first in _git(repo, "branch", "--list", first)
