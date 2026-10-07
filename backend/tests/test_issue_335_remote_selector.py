"""Selector parity for remote chats (issue #335, spec #332).

The full selector surface for remote chats, flipped from refusal to
service through the #333 gateway:

- Pin at creation: an explicit draft pick pre-stores; with no pick the
  HOST workspace's then-current branch is read through the channel and
  pinned 'inherited' (the local rule, remote twin). Host unreachable at
  creation -> no pin; the existing lazy-pin-on-first-read path covers it
  when the host comes back.
- Selector flips - the HTTP endpoint and the branch_select agent tool -
  perform a REAL dirty-refusing checkout inside the remote chat worktree
  (materializing it when missing), then record the explicit pin.
- A stale pin (branch gone from the host repo) is surfaced by the
  git-branch endpoint, never silently re-created or re-pointed.
- The branch dropdown endpoints serve host branch lists through the
  gateway; offline hosts give the same explicit answers they give the
  git-info endpoint.

Local selector behavior is unchanged everywhere (the #302 tri-state
suite keeps guarding it).
"""
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.agent.gitinfo import invalidate_git_caches
from backend.db.database import (
    _lazy_pin_attempted,
    create_conversation,
    get_conversation,
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
    exactly as RemoteSession.exec_tool does on connection failure. Takes
    a host_id so a test can replace a live session for the SAME
    workspace string (registrations are keyed by host_id)."""

    def __init__(self, host_id="h-off"):
        self.host_id = host_id

    async def exec_tool(self, name, args, workspace=""):
        return {"error": "remote host unreachable: ConnectError: boom"}


class _FakeHostSession:
    """A host whose repository is a real local repo the test prepared.
    exec_tool dispatches workspace tools the way the host's
    /api/remote/exec does (bash runs IN the workspace; file tools take
    workspace-relative paths) and returns run_bash's wire shape."""

    host_id = "h-335"

    def __init__(self, host_repo, host_id="h-335"):
        self.host_id = host_id
        self.host_repo = str(host_repo)
        self.commands = []
        self.tool_calls = []

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
def _clear_sessions():
    from backend.agent import remote as remote_mod

    remote_mod.clear_remote()
    yield
    remote_mod.clear_remote()


@pytest.fixture(autouse=True)
def _fresh_lazy_pins():
    """The lazy-pin dedup dict is process-global and keyed on (DB_PATH,
    conv id); a fresh throwaway DB reuses ids across tests."""
    _lazy_pin_attempted.clear()
    yield
    _lazy_pin_attempted.clear()


@pytest.fixture(autouse=True)
def _fresh_caches():
    invalidate_git_caches("whatever-root")
    yield
    invalidate_git_caches("whatever-root")


@pytest.fixture()
def _no_branch_cache():
    """A test that mutates host git state behind the TTL cache drops the
    branch-list/branch caches explicitly (the suites' invalidation is
    read-path-driven; a raw host-side `git checkout` bypasses it)."""
    from backend.agent import gitinfo

    gitinfo._branch_list_cache.clear()
    gitinfo._cache.clear()
    gitinfo._branch_times.clear()
    yield
    gitinfo._branch_list_cache.clear()
    gitinfo._cache.clear()
    gitinfo._branch_times.clear()


@pytest.fixture()
def client():
    from backend.main import app

    with TestClient(app) as c:
        yield c


def _ns(remote_mod, session, repo):
    return remote_mod.ns_path(session.host_id, str(repo))


# ---- pin at creation: inherited from the host, read through the channel ----


@pytest.mark.asyncio
async def test_creation_inherits_host_branch(tmp_path, _clear_sessions):
    from backend.agent import remote as remote_mod

    repo = _repo_with_commit(tmp_path, "host-create")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation("t", workspace=_ns(remote_mod, session, repo))
    conv = await get_conversation(cid)
    assert conv["selected_branch"] == "master"
    assert conv["branch_pin_origin"] == "inherited"


@pytest.mark.asyncio
async def test_creation_explicit_pick_wins_over_channel(tmp_path, _clear_sessions):
    from backend.agent import remote as remote_mod

    repo = _repo_with_commit(tmp_path, "host-explicit")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation(
        "t", workspace=_ns(remote_mod, session, repo), selected_branch="feature"
    )
    conv = await get_conversation(cid)
    assert conv["selected_branch"] == "feature"
    assert conv["branch_pin_origin"] == "explicit"


@pytest.mark.asyncio
async def test_creation_host_offline_no_pin_lazy_path_later(tmp_path, _clear_sessions):
    from backend.agent import remote as remote_mod

    repo = _repo_with_commit(tmp_path, "host-lazy")
    offline = _OfflineSession()
    remote_mod.register_remote(offline)
    cid = await create_conversation(
        "t", workspace=_ns(remote_mod, offline, repo)
    )
    conv = await get_conversation(cid)
    assert conv["selected_branch"] is None
    assert conv["branch_pin_origin"] is None
    # The host comes back: the lazy pin on first read covers it. The
    # recovered session keeps the SAME host_id, so the workspace string
    # (and the lazy dedup dict, cleared here for a fresh read) line up.
    recovered = _FakeHostSession(repo, host_id=offline.host_id)
    remote_mod.register_remote(recovered)
    _lazy_pin_attempted.clear()
    invalidate_git_caches(_ns(remote_mod, recovered, repo))
    conv = await get_conversation(cid)
    assert conv["selected_branch"] == "master"
    assert conv["branch_pin_origin"] == "inherited"


@pytest.mark.asyncio
async def test_creation_host_offline_no_probe_spam(tmp_path, _clear_sessions):
    """The unreachable-host creation path must not pay a channel timeout
    per conversation: no exec happens at all (the offline posture is
    explicit, cheap absence)."""
    from backend.agent import remote as remote_mod

    repo = _repo_with_commit(tmp_path, "host-cheap")
    session = _OfflineSession()
    remote_mod.register_remote(session)
    cid = await create_conversation(
        "t", workspace=_ns(remote_mod, session, repo)
    )
    conv = await get_conversation(cid)
    assert conv["selected_branch"] is None


# ---- selector flip endpoint: a real checkout in the remote chat worktree ----


@pytest.mark.asyncio
async def test_flip_endpoint_checks_out_in_remote_chat_worktree(
    tmp_path, _clear_sessions, client
):
    from backend.agent import remote as remote_mod
    from backend.agent import wt_remote

    repo = _repo_with_commit(tmp_path, "host-flip")
    _git(repo, "branch", "feature")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation("t", workspace=_ns(remote_mod, session, repo))

    r = client.post(
        f"/api/conversations/{cid}/branch-select", json={"branch": "feature"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["pin_origin"] == "explicit"

    # The chat worktree now exists ON THE HOST and sits on the branch.
    assert (repo / ".scratch" / "remote" / f"chat-{cid}" / ".git").exists()
    branch = _git(
        repo, "worktree", "list", "--porcelain"
    )
    assert f"chat-{cid}" in branch
    head = _git(
        repo, "-C", str(repo / ".scratch" / "remote" / f"chat-{cid}"),
        "rev-parse", "--abbrev-ref", "HEAD",
    )
    assert head == "feature"

    conv = await get_conversation(cid)
    assert conv["selected_branch"] == "feature"
    assert conv["branch_pin_origin"] == "explicit"


@pytest.mark.asyncio
async def test_flip_endpoint_refuses_dirty_chat_worktree(
    tmp_path, _clear_sessions, client
):
    from backend.agent import remote as remote_mod

    repo = _repo_with_commit(tmp_path, "host-dirty")
    _git(repo, "branch", "feature")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation("t", workspace=_ns(remote_mod, session, repo))

    # Materialize the chat tree, then dirty it on the host.
    mat = await wt_remote_ensure(repo, cid, "master")
    assert mat["path"] is not None
    chat_dir = repo / ".scratch" / "remote" / f"chat-{cid}"
    (chat_dir / "mess.txt").write_text("uncommitted\n", encoding="utf-8")

    r = client.post(
        f"/api/conversations/{cid}/branch-select", json={"branch": "feature"}
    )
    body = r.json()
    assert body["ok"] is False
    assert "uncommitted" in body["error"]
    conv = await get_conversation(cid)
    assert conv["selected_branch"] in (None, "master")  # never "feature"


async def wt_remote_ensure(repo, cid, branch):
    from backend.agent import wt_remote

    from backend.agent.remote import ns_path

    return await wt_remote.ensure_chat_worktree(
        ns_path("h-335", str(repo)), cid, branch
    )


@pytest.mark.asyncio
async def test_flip_refused_when_git_refuses_the_probe(
    tmp_path, _clear_sessions, client
):
    """Fail closed: when git itself refuses the cleanliness probe on a
    REACHABLE host (corrupt repo, locked index), the flip is refused —
    it never proceeds unchecked."""
    from backend.agent import remote as remote_mod
    from backend.agent import wt_remote

    repo = _repo_with_commit(tmp_path, "host-failclosed")
    _git(repo, "branch", "feature")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation("t", workspace=_ns(remote_mod, session, repo))
    mat = await wt_remote_ensure(repo, cid, "master")
    assert mat["path"] is not None

    # Sabotage the probe: a corrupt worktree index makes
    # `git -C <tree> status` fail while `worktree list` still succeeds —
    # the transient-corruption case the fail-closed rule exists for.
    admin_index = (
        repo / ".git" / "worktrees" / f"chat-{cid}" / "index"
    )
    admin_index.write_bytes(b"GARBAGEJUNK")

    r = client.post(
        f"/api/conversations/{cid}/branch-select", json={"branch": "feature"}
    )
    body = r.json()
    assert body["ok"] is False
    assert body["error"]  # git's refusal is surfaced, not swallowed
    conv = await get_conversation(cid)
    assert conv["selected_branch"] in (None, "master")


@pytest.mark.asyncio
async def test_tool_create_without_start_point_refuses(
    tmp_path, _clear_sessions, _no_branch_cache
):
    """A missing branch with NO derivable start (no pin, detached host
    HEAD) is refused explicitly — never an accidental detached
    materialization at the primary's HEAD."""
    from backend.agent import remote as remote_mod
    from backend.agent import gitinfo
    from backend.agent.tools import branch_select
    from backend.db.database import get_db

    repo = _repo_with_commit(tmp_path, "host-nostart")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation("t", workspace=_ns(remote_mod, session, repo))
    # Strip the pin raw (the lazy pin would otherwise have pinned the
    # inherited master before this test detached the primary).
    db = await get_db()
    try:
        await db.execute(
            "UPDATE conversations SET selected_branch = NULL,"
            " branch_pin_origin = NULL WHERE id = ?",
            (cid,),
        )
        await db.commit()
    finally:
        await db.close()
    _git(repo, "checkout", "--detach", "HEAD")
    gitinfo.invalidate_git_caches(_ns(remote_mod, session, repo))

    out = await branch_select(
        branch="fresh", conversation_id=cid,
        workspace=_ns(remote_mod, session, repo),
    )
    assert out["ok"] is False
    assert "fresh" in out["error"]
    assert not (repo / ".scratch" / "remote" / f"chat-{cid}").exists()


@pytest.mark.asyncio
async def test_flip_endpoint_refuses_branch_gone_from_host(
    tmp_path, _clear_sessions, client
):
    from backend.agent import remote as remote_mod

    repo = _repo_with_commit(tmp_path, "host-gone")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation("t", workspace=_ns(remote_mod, session, repo))

    r = client.post(
        f"/api/conversations/{cid}/branch-select", json={"branch": "nope"}
    )
    body = r.json()
    assert body["ok"] is False
    assert "nope" in body["error"]
    # No worktree materialized for a refused flip.
    assert not (repo / ".scratch" / "remote" / f"chat-{cid}").exists()


@pytest.mark.asyncio
async def test_flip_endpoint_host_offline_explicit(
    tmp_path, _clear_sessions, client
):
    from backend.agent import remote as remote_mod

    repo = _repo_with_commit(tmp_path, "host-off")
    session = _OfflineSession()
    remote_mod.register_remote(session)
    cid = await create_conversation("t", workspace=_ns(remote_mod, session, repo))

    r = client.post(
        f"/api/conversations/{cid}/branch-select", json={"branch": "master"}
    )
    body = r.json()
    assert body["ok"] is False
    assert "unreachable" in body["error"].lower()
    conv = await get_conversation(cid)
    assert conv["selected_branch"] is None  # the flip never stored


# ---- branch_select tool: the same real flip, tool surface ----


@pytest.mark.asyncio
async def test_tool_flip_checks_out_and_pins(tmp_path, _clear_sessions):
    from backend.agent import remote as remote_mod
    from backend.agent.tools import branch_select

    repo = _repo_with_commit(tmp_path, "host-tool")
    _git(repo, "branch", "feature")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation("t", workspace=_ns(remote_mod, session, repo))

    out = await branch_select(
        branch="feature", conversation_id=cid,
        workspace=_ns(remote_mod, session, repo),
    )
    assert out["ok"] is True
    assert out["created"] is False
    conv = await get_conversation(cid)
    assert conv["selected_branch"] == "feature"
    assert conv["branch_pin_origin"] == "explicit"
    chat_dir = repo / ".scratch" / "remote" / f"chat-{cid}"
    assert _git(repo, "-C", str(chat_dir), "rev-parse", "--abbrev-ref", "HEAD") == (
        "feature"
    )


@pytest.mark.asyncio
async def test_tool_dirty_flip_refuses(tmp_path, _clear_sessions):
    from backend.agent import remote as remote_mod
    from backend.agent.tools import branch_select

    repo = _repo_with_commit(tmp_path, "host-tooldirty")
    _git(repo, "branch", "feature")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation("t", workspace=_ns(remote_mod, session, repo))

    mat = await wt_remote_ensure(repo, cid, "master")
    assert mat["path"] is not None
    chat_dir = repo / ".scratch" / "remote" / f"chat-{cid}"
    (chat_dir / "mess.txt").write_text("x\n", encoding="utf-8")

    out = await branch_select(
        branch="feature", conversation_id=cid,
        workspace=_ns(remote_mod, session, repo),
    )
    assert out["ok"] is False
    assert "uncommitted" in out["error"]


@pytest.mark.asyncio
async def test_tool_creates_missing_branch_from_current_pin(
    tmp_path, _clear_sessions
):
    from backend.agent import remote as remote_mod
    from backend.agent.tools import branch_select

    repo = _repo_with_commit(tmp_path, "host-newb")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation("t", workspace=_ns(remote_mod, session, repo))

    out = await branch_select(
        branch="fresh", conversation_id=cid,
        workspace=_ns(remote_mod, session, repo),
    )
    assert out["ok"] is True
    assert out["created"] is True
    branches = _git(repo, "branch", "--format=%(refname:short)").split()
    assert "fresh" in branches
    conv = await get_conversation(cid)
    assert conv["selected_branch"] == "fresh"


@pytest.mark.asyncio
async def test_tool_create_false_and_missing_refuses(tmp_path, _clear_sessions):
    from backend.agent import remote as remote_mod
    from backend.agent.tools import branch_select

    repo = _repo_with_commit(tmp_path, "host-nocreate")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation("t", workspace=_ns(remote_mod, session, repo))

    out = await branch_select(
        branch="nope", create=False, conversation_id=cid,
        workspace=_ns(remote_mod, session, repo),
    )
    assert out["ok"] is False
    assert "not a local branch" in out["error"]


# ---- git-branch endpoint: stale detection through the gateway ----


@pytest.mark.asyncio
async def test_git_branch_remote_stale_flag(tmp_path, _clear_sessions, client):
    from backend.agent import remote as remote_mod

    repo = _repo_with_commit(tmp_path, "host-stale")
    _git(repo, "branch", "doomed")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation(
        "t", workspace=_ns(remote_mod, session, repo), selected_branch="doomed"
    )
    invalidate_git_caches(_ns(remote_mod, session, repo))

    body = client.get(f"/api/conversations/{cid}/git-branch").json()
    assert body == {
        "branch": "doomed", "pin_origin": "explicit", "stale": False,
    }

    # The branch dies on the host: the pin is surfaced stale, never
    # re-created or re-pointed.
    _git(repo, "branch", "-D", "doomed")
    invalidate_git_caches(_ns(remote_mod, session, repo))
    body = client.get(f"/api/conversations/{cid}/git-branch").json()
    assert body["stale"] is True
    assert body["branch"] == "doomed"  # the dead name is still surfaced
    conv = await get_conversation(cid)
    assert conv["selected_branch"] == "doomed"


@pytest.mark.asyncio
async def test_git_branch_remote_alive_not_stale(tmp_path, _clear_sessions, client):
    from backend.agent import remote as remote_mod

    repo = _repo_with_commit(tmp_path, "host-alive")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation(
        "t", workspace=_ns(remote_mod, session, repo), selected_branch="master"
    )
    invalidate_git_caches(_ns(remote_mod, session, repo))
    body = client.get(f"/api/conversations/{cid}/git-branch").json()
    assert body == {"branch": "master", "pin_origin": "explicit", "stale": False}


@pytest.mark.asyncio
async def test_git_branch_remote_offline_not_stale(tmp_path, _clear_sessions, client):
    from backend.agent import remote as remote_mod

    repo = _repo_with_commit(tmp_path, "host-offb")
    session = _OfflineSession()
    remote_mod.register_remote(session)
    cid = await create_conversation(
        "t", workspace=_ns(remote_mod, session, repo), selected_branch="master"
    )
    invalidate_git_caches(_ns(remote_mod, session, repo))
    body = client.get(f"/api/conversations/{cid}/git-branch").json()
    # Unreachable is not "branch deleted": no stale flag, dead-honest pin.
    assert body == {"branch": "master", "pin_origin": "explicit", "stale": False}


@pytest.mark.asyncio
async def test_git_branch_remote_no_pin_offline_reports_hostless_null(
    tmp_path, _clear_sessions, client
):
    """No pin and an offline host: the no-pin arm reports None — the
    explicit hostless state (the lazy pin will cover the row when the
    host returns; a re-pin of a NULL row when the host IS online is the
    designed lazy-pin behavior, not this endpoint's fallback arm)."""
    from backend.agent import remote as remote_mod

    repo = _repo_with_commit(tmp_path, "host-fallback")
    _git(repo, "branch", "dev")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation(
        "t", workspace=_ns(remote_mod, session, repo), selected_branch="dev"
    )
    invalidate_git_caches(_ns(remote_mod, session, repo))
    body = client.get(f"/api/conversations/{cid}/git-branch").json()
    assert body["branch"] == "dev"

    from backend.db.database import get_db

    db = await get_db()
    try:
        await db.execute(
            "UPDATE conversations SET selected_branch = NULL,"
            " branch_pin_origin = NULL WHERE id = ?",
            (cid,),
        )
        await db.commit()
    finally:
        await db.close()
    # The host goes away before the read: the fallback arm runs (the
    # lazy pin cannot fire against an unreachable host) and reports None.
    remote_mod.register_remote(_OfflineSession(session.host_id))
    body = client.get(f"/api/conversations/{cid}/git-branch").json()
    assert body == {"branch": None, "pin_origin": None, "stale": False}


# ---- branch dropdown endpoints through the gateway ----


@pytest.mark.asyncio
async def test_git_branches_endpoint_lists_host_branches(
    tmp_path, _clear_sessions, client
):
    from backend.agent import remote as remote_mod

    repo = _repo_with_commit(tmp_path, "host-list")
    _git(repo, "branch", "feature")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    cid = await create_conversation("t", workspace=_ns(remote_mod, session, repo))
    invalidate_git_caches(_ns(remote_mod, session, repo))
    body = client.get(f"/api/conversations/{cid}/git-branches").json()
    assert sorted(body["branches"]) == ["feature", "master"]


@pytest.mark.asyncio
async def test_git_branches_endpoint_offline_empty(tmp_path, _clear_sessions, client):
    from backend.agent import remote as remote_mod

    session = _OfflineSession()
    remote_mod.register_remote(session)
    cid = await create_conversation(
        "t", workspace=_ns(remote_mod, session, "C:/repo")
    )
    body = client.get(f"/api/conversations/{cid}/git-branches").json()
    assert body == {"branches": []}


@pytest.mark.asyncio
async def test_workspace_git_branches_endpoint_serves_remote(
    tmp_path, _clear_sessions, client
):
    from backend.agent import remote as remote_mod

    repo = _repo_with_commit(tmp_path, "host-draft")
    _git(repo, "branch", "feature")
    session = _FakeHostSession(repo)
    remote_mod.register_remote(session)
    invalidate_git_caches(_ns(remote_mod, session, repo))
    body = client.get(
        "/api/workspaces/git-branches?workspace="
        + remote_mod.ns_path(session.host_id, str(repo))
    ).json()
    assert body["branch"] == "master"
    assert sorted(body["branches"]) == ["feature", "master"]


@pytest.mark.asyncio
async def test_workspace_git_branches_endpoint_offline(
    tmp_path, _clear_sessions, client
):
    from backend.agent import remote as remote_mod

    session = _OfflineSession()
    remote_mod.register_remote(session)
    body = client.get(
        "/api/workspaces/git-branches?workspace="
        + remote_mod.ns_path(session.host_id, "C:/repo")
    ).json()
    # #332 (UI parity): offline is an EXPLICIT state on the draft card,
    # not a silent empty answer.
    assert body == {"branch": None, "branches": [], "offline": True}


@pytest.mark.asyncio
async def test_draft_chip_offline_explicit_never_fabricates(
    tmp_path, _clear_sessions, client
):
    """Offline draft read: the explicit offline state, never a fabricated
    branch name (the card renders 'host offline', not a picker)."""
    from backend.agent import remote as remote_mod

    session = _OfflineSession()
    remote_mod.register_remote(session)
    body = client.get(
        "/api/workspaces/git-branches?workspace="
        + remote_mod.ns_path(session.host_id, "C:/repo")
    ).json()
    assert body["offline"] is True
    assert body["branch"] is None and body["branches"] == []


# ---- local behavior unchanged (guard rails around the flip) ----


@pytest.mark.asyncio
async def test_local_creation_still_pins_inherited_locally(tmp_path):
    """No remote session registered: a local workspace pins exactly as
    before (the #302 suite's own scenario, re-guarded next to the flip)."""
    repo = _repo_with_commit(tmp_path, "local-still")
    cid = await create_conversation("t", workspace=str(repo))
    conv = await get_conversation(cid)
    assert conv["selected_branch"] == "master"
    assert conv["branch_pin_origin"] == "inherited"
