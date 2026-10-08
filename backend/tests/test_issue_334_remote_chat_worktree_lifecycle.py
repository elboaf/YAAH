"""Chat worktree lifecycle on the host for remote chats (issue #334,
spec #332; ADR-0010 remote-parity amendment).

A remote chat's worktree materializes on the HOST, in the client-owned
nested namespace at its selected branch, on first write; landing is a
plain merge inside that tree; retirement follows #329 semantics;
materialization failure degrades per the universal rule (#322); the
namespace is invisible to the host instance's sweeper and to host git
status; legacy remote chats (creation-time pin only) materialize on
their next run.

Every git operation rides the #333 gateway; tests inject a fake channel
session (the _StubSession pattern from test_issue_333) whose host repo
is a REAL local repo — the wire shape is run_bash's:
{exit_code, output, timed_out, ...} or {error}. The file-tool executors
(read_file/write_file/delete_file) are dispatched the same way the host
dispatches /api/remote/exec, so the exclude write and the husk prune
exercise the real host-side code paths.
"""
import os
import subprocess

import pytest


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
    windows = False

    def env_line(self, workspace=""):
        return "Runtime environment: test-fake offline host."

    async def exec_tool(self, name, args, workspace=""):
        return {"error": "remote host unreachable: ConnectError: boom"}


class _FakeHostSession:
    """A host whose repository is a real local repo the test prepared.
    exec_tool dispatches workspace tools the way the host's
    /api/remote/exec does (bash runs IN the workspace; file tools take
    workspace-relative paths) and returns run_bash's wire shape."""

    host_id = "h-fake"
    windows = False

    def __init__(self, host_repo):
        self.host_repo = str(host_repo)
        self.commands = []
        self.tool_calls = []

    def env_line(self, workspace=""):
        return (
            "Runtime environment: test-fake on the remote host 'fake'. "
            "The bash tool runs commands there; file and shell tools "
            "operate there, not on this machine."
        )

    async def exec_tool(self, name, args, workspace=""):
        self.tool_calls.append((name, args, workspace))
        if name == "bash":
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
        # The file-tool executors, dispatched host-side exactly like
        # api_remote_exec does (fn(workspace=..., **args)).
        from backend.agent import tools as tools_mod

        fn = tools_mod.EXECUTORS.get(name)
        assert fn is not None, f"tool {name} missing from EXECUTORS"
        return await fn(workspace=self.host_repo, **args)


@pytest.fixture()
def _sessions():
    from backend.agent import remote as remote_mod

    remote_mod.clear_remote()
    yield remote_mod
    remote_mod.clear_remote()


@pytest.fixture()
def _quiet_notes():
    """Blank AGENTS.md/memory notes for run-loop tests, restored after —
    the loop functions are module state; leaking a lambda would poison
    every later test in the shard."""
    from backend.agent import loop

    saved = (loop._agents_notes_async, loop._memory_notes)
    loop._memory_notes = lambda w: ""

    async def _no_notes(workspace):
        return ""

    loop._agents_notes_async = _no_notes
    yield loop
    loop._agents_notes_async, loop._memory_notes = saved


@pytest.fixture()
def _fake_model():
    """Swap the model client for a canned stream, restored after."""
    from backend.agent import loop

    captured = {}

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        captured["system"] = messages[0]["content"]

        async def _stream():
            yield {"type": "content", "text": "done"}
            yield {"type": "finish"}
        return _stream()

    saved = loop.model_client.chat
    loop.model_client.chat = fake_chat
    yield captured
    loop.model_client.chat = saved


def _register(remote_mod, session):
    remote_mod.register_remote(session)
    return session


def _ws(host_id, repo):
    from backend.agent.remote import ns_path

    return ns_path(host_id, str(repo))


# ------------------------------------------------------- materialization


@pytest.mark.asyncio
async def test_materializes_on_host_at_selected_branch(_sessions, tmp_path):
    """AC: first write of a remote run materializes the chat worktree on
    the host under .scratch/remote/chat-<id>/ at the chat's selected
    branch (through the gateway; fake channel)."""
    from backend.agent import wt_remote

    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    session = _register(_sessions, _FakeHostSession(repo))
    ws = _ws(session.host_id, repo)

    out = await wt_remote.ensure_chat_worktree(ws, 7, "feature")
    assert out["path"] is not None, out
    chat_dir = repo / ".scratch" / "remote" / "chat-7"
    assert chat_dir.exists()
    assert out["created"] is True
    assert out["detached"] is False
    # Really on the selected branch.
    head = _git(chat_dir, "symbolic-ref", "-q", "HEAD")
    assert head == "refs/heads/feature"
    # The client wrote the exclude rule itself (placement criterion:
    # the exclude write goes through the channel).
    exclude = repo / ".git" / "info" / "exclude"
    assert ".scratch/" in exclude.read_text(encoding="utf-8")
    writes = [
        c for c in session.tool_calls if c[0] == "write_file"
        and "exclude" in c[1].get("path", "")
    ]
    assert writes, "exclude write must ride the channel file tool"


@pytest.mark.asyncio
async def test_detaches_when_branch_held_by_primary(_sessions, tmp_path):
    """The host's primary holds master (the common case): the chat tree
    detaches at the same tip — the one-checkout-per-branch fallback,
    identical to local."""
    from backend.agent import wt_remote

    repo = _repo_with_commit(tmp_path)  # master checked out in the primary
    session = _register(_sessions, _FakeHostSession(repo))
    ws = _ws(session.host_id, repo)

    out = await wt_remote.ensure_chat_worktree(ws, 8, "master")
    assert out["path"] is not None, out
    assert out["detached"] is True
    chat_dir = repo / ".scratch" / "remote" / "chat-8"
    assert _git(repo, "rev-parse", "master") == _git(chat_dir, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_idempotent_second_call_flips_in_place(_sessions, tmp_path):
    """Legacy remote chats (a creation-time pin, never materialized) and
    any second run arrive here again: existing tree -> flip = checkout
    inside the chat's own worktree; same run returns created=False."""
    from backend.agent import wt_remote

    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    session = _register(_sessions, _FakeHostSession(repo))
    ws = _ws(session.host_id, repo)

    first = await wt_remote.ensure_chat_worktree(ws, 9, "feature")
    assert first["created"] is True
    second = await wt_remote.ensure_chat_worktree(ws, 9, "feature")
    assert second["created"] is False
    assert second["path"] == first["path"]

    # A selector flip to another branch really moves the chat tree.
    _git(repo, "branch", "topic")
    flipped = await wt_remote.ensure_chat_worktree(ws, 9, "topic")
    assert flipped["detached"] is False
    head = _git(repo / ".scratch" / "remote" / "chat-9", "symbolic-ref", "-q", "HEAD")
    assert head == "refs/heads/topic"


@pytest.mark.asyncio
async def test_offline_host_is_explicit_error_not_crash(_sessions, tmp_path):
    """AC: materialization failure -> the universal degraded-mode rule:
    an explicit error dict, never a raise. (The run loop turns this into
    the degraded note.)"""
    from backend.agent import wt_remote

    _register(_sessions, _OfflineSession())
    ws = _ws("h-offline", "C:/repo")
    out = await wt_remote.ensure_chat_worktree(ws, 10, "master")
    assert out["path"] is None
    assert out.get("error")


@pytest.mark.asyncio
async def test_unknown_host_is_explicit_error(_sessions, tmp_path):
    from backend.agent import wt_remote

    out = await wt_remote.ensure_chat_worktree("remote:h-ghost:C:/repo", 11, "master")
    assert out["path"] is None and out.get("error")


@pytest.mark.asyncio
async def test_unborn_repo_gets_gated_orphan_worktree(_sessions, tmp_path):
    """#331 parity: an unborn HEAD (fresh init) is a real repo — the
    UNBORN-GATED orphan fallback creates the chat worktree on the pinned
    branch name, exactly like the local materializer. (The explicit
    'no commits yet' error is the OLD-git fallback, covered locally.)"""
    from backend.agent import wt_remote

    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    session = _register(_sessions, _FakeHostSession(repo))
    ws = _ws(session.host_id, repo)

    out = await wt_remote.ensure_chat_worktree(ws, 12, "master")
    assert out.get("error") is None, out
    assert out["created"] is True and out["detached"] is False
    chat_dir = repo / ".scratch" / "remote" / "chat-12"
    assert chat_dir.exists()
    head = _git(chat_dir, "symbolic-ref", "-q", "HEAD")
    assert head == "refs/heads/master"


# ------------------------------------------------------- landing + retire


@pytest.mark.asyncio
async def test_landed_clean_run_end_retires_the_chat_tree(_sessions, tmp_path):
    """AC: a landed-clean run end retires the chat worktree. The agent's
    SOP is followed literally (run worktree inside the chat tree, work,
    merge onto the selected branch, remove the run tree), then the
    post-run hook retires the chat tree."""
    from backend.agent import wt_remote

    repo = _repo_with_commit(tmp_path)
    session = _register(_sessions, _FakeHostSession(repo))
    ws = _ws(session.host_id, repo)

    out = await wt_remote.ensure_chat_worktree(ws, 21, "master")
    assert out["path"] is not None, out
    chat_dir = repo / ".scratch" / "remote" / "chat-21"

    # The agent lands per SOP, from INSIDE the chat tree.
    _git(chat_dir, "worktree", "add", "-b", "run/work-21", ".scratch/chat-21/run")
    run_dir = chat_dir / ".scratch" / "chat-21" / "run"
    (run_dir / "work.txt").write_text("worked\n", encoding="utf-8")
    _git(run_dir, "add", "-A")
    _git(run_dir, "commit", "-q", "-m", "work")
    _git(chat_dir, "merge", "--no-ff", "-m", "land run/work-21", "run/work-21")
    _git(chat_dir, "worktree", "remove", ".scratch/chat-21/run")

    res = await wt_remote.retire_chat_worktree(ws, 21)
    assert res.get("retired") is True, res
    assert not chat_dir.exists()
    # No empty-husk left behind: the namespace is pruned back.
    assert not (repo / ".scratch" / "remote" / "chat-21").exists()
    assert chat_dir.as_posix() not in _git(repo, "worktree", "list", "--porcelain")


@pytest.mark.asyncio
async def test_dirty_tree_survives_retirement(_sessions, tmp_path):
    from backend.agent import wt_remote

    repo = _repo_with_commit(tmp_path)
    session = _register(_sessions, _FakeHostSession(repo))
    ws = _ws(session.host_id, repo)

    await wt_remote.ensure_chat_worktree(ws, 22, "master")
    chat_dir = repo / ".scratch" / "remote" / "chat-22"
    (chat_dir / "uncommitted.txt").write_text("wip", encoding="utf-8")

    res = await wt_remote.retire_chat_worktree(ws, 22)
    assert res.get("retired") is False
    assert res.get("reason") == "dirty"
    assert chat_dir.exists()


@pytest.mark.asyncio
async def test_residue_holding_tree_survives_retirement(_sessions, tmp_path):
    """#329/#330 semantics: the run namespace is git-invisible, so
    residue is pinned by the pathspec-limited ignored-file probe — a
    standing run worktree keeps the chat tree."""
    from backend.agent import wt_remote

    repo = _repo_with_commit(tmp_path)
    session = _register(_sessions, _FakeHostSession(repo))
    ws = _ws(session.host_id, repo)

    await wt_remote.ensure_chat_worktree(ws, 23, "master")
    chat_dir = repo / ".scratch" / "remote" / "chat-23"
    _git(chat_dir, "worktree", "add", "-b", "run/work-23", ".scratch/chat-23/run")

    res = await wt_remote.retire_chat_worktree(ws, 23)
    assert res.get("retired") is False
    assert res.get("reason") == "run residue present"
    assert chat_dir.exists()


@pytest.mark.asyncio
async def test_retire_refuses_when_host_offline(_sessions, tmp_path):
    """Unreachable != retired: the tree stands and the next run retries."""
    from backend.agent import wt_remote

    repo = _repo_with_commit(tmp_path)
    session = _register(_sessions, _FakeHostSession(repo))
    ws = _ws(session.host_id, repo)
    await wt_remote.ensure_chat_worktree(ws, 24, "master")

    _sessions.register_remote(_OfflineSession())
    ws_off = _ws("h-offline", str(repo))
    res = await wt_remote.retire_chat_worktree(ws_off, 24)
    assert res.get("retired") is False
    assert res.get("reason") == "host unreachable"
    assert (repo / ".scratch" / "remote" / "chat-24").exists()


# ------------------------------------------------- placement (invisibility)


@pytest.mark.asyncio
async def test_host_sweeper_cannot_see_the_guest_namespace(_sessions, tmp_path):
    """AC (placement): the HOST instance's sweeper discovery pass cannot
    match the guest namespace. The sweeper's one-level .scratch/chat-*
    glob finds nothing under .scratch/remote/, even with a dead chat id
    planted there and a live local chat tree standing next to it."""
    from backend.agent import wt_remote, wt_sweep
    from backend.db.database import get_db

    repo = _repo_with_commit(tmp_path)
    session = _register(_sessions, _FakeHostSession(repo))
    ws = _ws(session.host_id, repo)

    await wt_remote.ensure_chat_worktree(ws, 31, "master")  # guest tree
    # The workspace row exists (the host knows the repo); the guest chat
    # id has NO conversation row anywhere (dead, aged).
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO workspaces (path, label) VALUES (?, ?)",
            (str(repo), "host-repo"),
        )
        await db.commit()
    finally:
        await db.close()

    # Age the whole namespace past the threshold to make the sweep
    # aggressive.
    old = __import__("time").time() - 40 * 24 * 3600
    os.utime(repo / ".scratch" / "remote", (old, old))

    result = await wt_sweep.sweep_stale_chat_worktrees()
    assert (repo / ".scratch" / "remote" / "chat-31").exists(), (
        "the guest tree must survive a host sweep"
    )
    assert not any("remote" in p for p in result.get("swept", []))


@pytest.mark.asyncio
async def test_namespace_is_git_invisible_on_the_host(_sessions, tmp_path):
    """AC (placement): the namespace stays git-invisible on the host —
    the exclude write goes through the channel; a materialized chat tree
    with a nested run worktree leaves `git status --porcelain` EMPTY
    from the host repo root."""
    from backend.agent import wt_remote

    repo = _repo_with_commit(tmp_path)
    session = _register(_sessions, _FakeHostSession(repo))
    ws = _ws(session.host_id, repo)

    await wt_remote.ensure_chat_worktree(ws, 32, "master")
    chat_dir = repo / ".scratch" / "remote" / "chat-32"
    _git(chat_dir, "worktree", "add", "-b", "run/work-32", ".scratch/chat-32/run")
    (chat_dir / ".scratch" / "chat-32" / "run" / "scratch.txt").write_text(
        "x", encoding="utf-8"
    )
    _git(chat_dir, "status", "--porcelain")  # chat tree itself is clean

    assert _git(repo, "status", "--porcelain") == ""


# ------------------------------------------------- legacy pin + run loop


@pytest.mark.asyncio
async def test_legacy_pin_materializes_on_next_run(
    _sessions, _quiet_notes, _fake_model, tmp_path,
):
    """AC: a legacy remote chat (only a creation-time pin) materializes
    its worktree on its next run — the run loop's re-point does it, the
    prompt carries the selector SOP, and the run executes inside the
    chat tree. (A clean no-op run then retires the tree at run end per
    #329 — covered by the retire tests; here we pin the re-point itself
    by blocking retirement with a standing run worktree.)"""
    from backend.db.database import create_conversation, update_conversation
    from backend.agent import loop

    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    session = _register(_sessions, _FakeHostSession(repo))
    ws = _ws(session.host_id, repo)

    cid = await create_conversation("legacy", ws)
    # The creation-time pin — stored, never materialized until now.
    await update_conversation(cid, selected_branch="feature")
    async for _ in loop.run_agent(cid, "go", ws):
        pass

    chat_dir = repo / ".scratch" / "remote" / f"chat-{cid}"
    # The worktree was materialized on the host during the run (the
    # channel saw the add) and the run's tools were re-pointed at the
    # namespaced chat tree.
    adds = [
        c for c in session.commands
        if c.startswith("git worktree add") and f"chat-{cid}" in c
    ]
    assert adds, f"legacy chat's worktree must materialize on its next run: {session.commands}"
    # #360 (ADR-0017): the run SOP and selector note no longer reach the
    # prompt; the physical materialization asserts above still pin that.
    remote_ws = [
        c for c in session.tool_calls
        if c[0] == "bash" and str(c[2]).startswith("remote:")
    ]
    assert remote_ws, "tool calls must ride the namespaced workspace"
    # The run was a clean no-op, so #329 retired the tree at run end.
    assert not chat_dir.exists()


@pytest.mark.asyncio
async def test_offline_materialization_degrades_the_run(
    _sessions, _quiet_notes, _fake_model, tmp_path,
):
    """AC: materialization failure over the channel never crashes the
    run (#360/ADR-0017: the degraded-note contract is gone; only the
    no-crash behavior remains to pin)."""
    from backend.db.database import create_conversation, update_conversation
    from backend.agent import loop

    repo = _repo_with_commit(tmp_path)
    _register(_sessions, _OfflineSession())
    ws = _ws("h-offline", str(repo))

    cid = await create_conversation("degraded", ws)
    await update_conversation(cid, selected_branch="master")
    async for _ in loop.run_agent(cid, "go", ws):
        pass

    # #360 (ADR-0017): the degraded-mode note no longer reaches the
    # prompt; the physical contract left is that an offline host fails
    # materialization without crashing the run.
    assert _fake_model["system"], "the run still completed"


@pytest.mark.asyncio
async def test_agents_md_fetched_from_host_for_remote_run(
    _sessions, _fake_model, monkeypatch, tmp_path,
):
    """Injection parity prerequisite (#334 groundwork here, full surface
    in #336): the host's AGENTS.md reaches the prompt through the
    channel, missing file degrades like local."""
    from backend.db.database import create_conversation
    from backend.agent import loop

    monkeypatch.setattr(loop, "_memory_notes", lambda w: "")
    repo = _repo_with_commit(tmp_path)
    (repo / "AGENTS.md").write_text(
        "# Project notes\n\nUse tabs. Never push to master.\n", encoding="utf-8"
    )
    session = _register(_sessions, _FakeHostSession(repo))
    ws = _ws(session.host_id, repo)

    cid = await create_conversation("agents-md", ws)
    async for _ in loop.run_agent(cid, "go", ws):
        pass

    assert "Never push to master." in _fake_model["system"]
