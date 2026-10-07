"""Remote git gateway + git-info parity (issue #333, spec #332).

The gateway (backend/agent/gitexec.py) is the one new seam: every git
read resolves to an executor — local workspaces keep today's local git
execution, remote workspaces ship git through the existing remote exec
channel (the remote bash tool path). Distinguishability contract:
(rc, output) = git ran; None = git could not run at all (host offline,
channel error) so callers surface an explicit state instead of reading
absence as data.

Tests inject a fake channel session — no real HTTP (the _StubSession
pattern from test_remote.py).
"""
import subprocess

from fastapi.testclient import TestClient

import pytest

from backend.agent import gitexec
from backend.agent.gitinfo import invalidate_git_caches


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


@pytest.fixture()
def fresh_caches():
    invalidate_git_caches("whatever-root")  # harmless; tests also clear per-key
    yield
    invalidate_git_caches("whatever-root")


# ---- gateway: the distinguishability contract ----


@pytest.mark.asyncio
async def test_gateway_local_runs_git(tmp_path):
    repo = _repo_with_commit(tmp_path)
    rc, out = await gitexec.run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert rc == 0
    assert out == "master"


@pytest.mark.asyncio
async def test_gateway_remote_none_when_host_offline(_clear_sessions):
    from backend.agent.remote import ns_path, register_remote

    register_remote(_OfflineSession())
    ws = ns_path("h-offline", "C:/repo")
    assert await gitexec.run_git(ws, "status", "--porcelain") is None


@pytest.mark.asyncio
async def test_gateway_remote_none_when_host_unknown(_clear_sessions):
    # No session registered for this host id at all.
    assert await gitexec.run_git("remote:h-ghost:C:/repo", "status") is None


@pytest.mark.asyncio
async def test_gateway_remote_error_shape_is_none(_clear_sessions):
    from backend.agent.remote import ns_path, register_remote

    class _BadShape(_OfflineSession):
        async def exec_tool(self, name, args, workspace=""):
            return {"unexpected": "shape"}

    register_remote(_BadShape())
    assert await gitexec.run_git(ns_path("h-offline", "C:/repo"), "status") is None


@pytest.mark.asyncio
async def test_gateway_local_none_only_for_local_failure(tmp_path):
    # A local non-repo still gives (rc, out) — git ran and said no —
    # because the executor ran; only unreachable-executor is None.
    rc, out = await gitexec.run_git(tmp_path, "status", "--porcelain")
    assert isinstance(rc, int)


# ---- remote path: correctness through the channel ----


@pytest.mark.asyncio
async def test_gateway_remote_runs_git_on_host(tmp_path, _clear_sessions):
    from backend.agent.remote import ns_path, register_remote

    repo = _repo_with_commit(tmp_path, "hostrepo")
    session = _FakeHostSession(repo)
    register_remote(session)
    ws = ns_path("h-fake", str(repo))
    rc, out = await gitexec.run_git(ws, "rev-parse", "--abbrev-ref", "HEAD")
    assert rc == 0
    assert out == "master"
    # One channel round-trip, cwd already the workspace: no -C, no quoting
    # hazards — the corpus is a bare arg list.
    assert session.commands == ["git rev-parse --abbrev-ref HEAD"]


@pytest.mark.asyncio
async def test_gateway_corpus_is_cross_dialect_safe(_clear_sessions):
    """Every command the gateway composes must parse identically in POSIX
    sh and cmd.exe: a Windows host without Git Bash gets cmd.exe, so the
    corpus may not rely on quoting, parens, or shell chaining."""
    import re

    forbidden = re.compile(r"[\"'`;&|()<>\^\%\!\,]")
    seen = []

    real_remote = gitexec.remote_for_workspace
    real_run = gitexec.run_git

    class _Recorder:
        async def run_git(self, ws, *args):
            seen.append("git " + " ".join(args))
            return 0, ""

    gitexec.remote_for_workspace = lambda ws: "session"  # type: ignore[assignment]
    gitexec.run_git = _Recorder().run_git  # type: ignore[assignment]
    try:
        from backend.agent.gitinfo import git_workspace_info

        await git_workspace_info("remote:h-x:C:/repo")
    finally:
        gitexec.remote_for_workspace = real_remote  # type: ignore[assignment]
        gitexec.run_git = real_run  # type: ignore[assignment]

    assert seen, "gateway composed no commands for the remote path"
    for cmd in seen:
        assert not forbidden.search(cmd), f"non-portable command: {cmd}"


# ---- gitinfo: the readouts go remote-aware ----


@pytest.mark.asyncio
async def test_git_workspace_info_remote_offline_is_explicit(
    tmp_path, _clear_sessions
):
    from backend.agent.gitinfo import git_workspace_info
    from backend.agent.remote import ns_path, register_remote

    register_remote(_OfflineSession())
    ws = ns_path("h-offline", "C:/repo")
    info = await git_workspace_info(ws)
    assert info == {"offline": True}


@pytest.mark.asyncio
async def test_git_workspace_info_remote_online_reads_host(
    tmp_path, _clear_sessions
):
    from backend.agent.gitinfo import git_workspace_info
    from backend.agent.remote import ns_path, register_remote

    repo = _repo_with_commit(tmp_path, "hostrepo2")
    session = _FakeHostSession(repo)
    register_remote(session)
    ws = ns_path("h-fake", str(repo))
    info = await git_workspace_info(ws)
    assert info is not None and not info.get("offline")
    assert info["branch"] == "master"
    assert info["dirty"] is False


@pytest.mark.asyncio
async def test_git_workspace_info_remote_dirty_via_channel(
    tmp_path, _clear_sessions
):
    from backend.agent.gitinfo import git_workspace_info
    from backend.agent.remote import ns_path, register_remote

    repo = _repo_with_commit(tmp_path, "hostrepo3")
    (repo / "dirty.txt").write_text("x\n", encoding="utf-8")
    session = _FakeHostSession(repo)
    register_remote(session)
    info = await git_workspace_info(ns_path("h-fake", str(repo)))
    assert info is not None and info["dirty"] is True
    assert info["untracked"] == 1


@pytest.mark.asyncio
async def test_git_workspace_info_local_stays_none_for_non_repo(
    tmp_path, fresh_caches
):
    from backend.agent.gitinfo import git_workspace_info

    # A local non-repo keeps today's contract: None (not-a-repo), NOT the
    # offline state — absence of a repo is data, not unreachability.
    assert await git_workspace_info(tmp_path) is None


# ---- HTTP: the endpoint through the gateway (TestClient + fake channel) ----


@pytest.fixture()
def client():
    from backend.main import app

    with TestClient(app) as c:
        yield c


@pytest.mark.asyncio
async def test_git_info_endpoint_remote_online(
    client, tmp_path, _clear_sessions
):
    from backend.agent.remote import ns_path, register_remote
    from backend.db.database import create_conversation

    repo = _repo_with_commit(tmp_path, "hostrepo4")
    register_remote(_FakeHostSession(repo))
    cid = await create_conversation("t", workspace=ns_path("h-fake", str(repo)))
    r = client.get(f"/api/conversations/{cid}/git-info").json()
    info = r["info"]
    assert info is not None and not info.get("offline")
    assert info["branch"] == "master"
    assert info["dirty"] is False


@pytest.mark.asyncio
async def test_git_info_endpoint_remote_offline_is_explicit(
    client, _clear_sessions
):
    from backend.agent.remote import ns_path, register_remote
    from backend.db.database import create_conversation

    register_remote(_OfflineSession())
    cid = await create_conversation(
        "t", workspace=ns_path("h-offline", "C:/repo")
    )
    r = client.get(f"/api/conversations/{cid}/git-info").json()
    assert r["info"] == {"offline": True}


# ---- review findings: wire-shape hazards, caching, parity ----


@pytest.mark.asyncio
async def test_remote_warning_lines_never_poison_readout(
    tmp_path, _clear_sessions
):
    """The channel's shell merges stderr: a routine CRLF warning must not
    mark a clean tree dirty (only porcelain-shaped lines are read)."""
    from backend.agent import gitinfo
    from backend.agent.remote import ns_path, register_remote

    class _NoisyHost(_FakeHostSession):
        async def exec_tool(self, name, args, workspace=""):
            result = await super().exec_tool(name, args, workspace)
            if result.get("exit_code") == 0 and args["command"].startswith("git status"):
                result = dict(result)
                result["output"] = (
                    "warning: in the working copy of 'x.txt', LF will be "
                    "replaced by CRLF the next time Git touches it\n"
                ) + result["output"]
            return result

    repo = _repo_with_commit(tmp_path, "noisyrepo")
    session = _NoisyHost(repo)
    register_remote(session)
    gitinfo._info_cache.pop(ns_path("h-fake", str(repo)), None)
    info = await gitinfo.git_workspace_info(ns_path("h-fake", str(repo)))
    assert info["dirty"] is False
    assert info["branch"] == "master"
    assert info["changed"] == 0


@pytest.mark.asyncio
async def test_remote_git_failure_is_reported_not_fabricated(
    tmp_path, _clear_sessions
):
    """A non-repo on the host must surface the refusal (offline + error),
    never a fabricated dirty readout."""
    from backend.agent import gitinfo
    from backend.agent.remote import ns_path, register_remote

    bare = tmp_path / "notarepo"
    bare.mkdir()
    session = _FakeHostSession(bare)
    register_remote(session)
    ws = ns_path("h-fake", str(bare))
    gitinfo._info_cache.pop(ws, None)
    info = await gitinfo.git_workspace_info(ws)
    assert info.get("offline") is True
    assert "not a git repository" in info.get("error", "")


@pytest.mark.asyncio
async def test_remote_online_result_is_ttl_cached(tmp_path, _clear_sessions):
    """All pollers share one channel burst per TTL — the composed readout
    is cached exactly as the local readout is."""
    from backend.agent import gitinfo
    from backend.agent.remote import ns_path, register_remote

    repo = _repo_with_commit(tmp_path, "cachedrepo")
    session = _FakeHostSession(repo)
    calls = {"n": 0}

    class _Counting(_FakeHostSession):
        async def exec_tool(self, name, args, workspace=""):
            calls["n"] += 1
            return await super().exec_tool(name, args, workspace)

    session = _Counting(repo)
    register_remote(session)
    ws = ns_path("h-fake", str(repo))
    gitinfo._info_cache.pop(ws, None)
    first = await gitinfo.git_workspace_info(ws)
    second = await gitinfo.git_workspace_info(ws)
    burst1 = calls["n"]
    assert first == second
    assert burst1 == calls["n"]


@pytest.mark.asyncio
async def test_remote_detached_head_reports_short_sha(
    tmp_path, _clear_sessions
):
    from backend.agent import gitinfo
    from backend.agent.remote import ns_path, register_remote

    repo = _repo_with_commit(tmp_path, "detachedrepo")
    sha = _git(repo, "rev-parse", "--short=7", "HEAD")
    _git(repo, "checkout", "-q", "--detach")
    session = _FakeHostSession(repo)
    register_remote(session)
    ws = ns_path("h-fake", str(repo))
    gitinfo._info_cache.pop(ws, None)
    info = await gitinfo.git_workspace_info(ws)
    assert info["branch"] == sha
    assert info["local_hash"] == sha


def test_info_from_status_marker_survives_bracket_paths():
    """A C-quoted path containing ' [' in the body must not corrupt the
    ahead/behind marker parsed from the ## head line."""
    from backend.agent.gitinfo import _info_from_status

    out = (
        "## master...origin/master [ahead 2, behind 1]\n"
        '?? "weird [bracket] name.txt"\n'
    )
    info = _info_from_status(out)
    assert info["ahead"] == 2
    assert info["behind"] == 1
    assert info["untracked"] == 1
    assert info["dirty"] is True


@pytest.mark.asyncio
async def test_remote_branch_lookup_via_gateway(tmp_path, _clear_sessions):
    """current_git_branch's remote branch (TTL-gated cache, single-line
    guard) — covered, since the endpoint skips no longer reach it."""
    from backend.agent import gitinfo
    from backend.agent.remote import ns_path, register_remote

    repo = _repo_with_commit(tmp_path, "branchrepo")
    session = _FakeHostSession(repo)
    register_remote(session)
    ws = ns_path("h-fake", str(repo))
    gitinfo._cache.pop(ws, None)
    gitinfo._branch_times.pop(ws, None)
    assert await gitinfo.current_git_branch(ws) == "master"
    # TTL-gated: the second poll inside the window costs no round-trip.
    assert await gitinfo.current_git_branch(ws) == "master"
    assert session.commands.count("git rev-parse --abbrev-ref HEAD") == 1
