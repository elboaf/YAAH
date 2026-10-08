"""Issue #350: the branch chip's sync readout shows the branch tip and its
upstream's tip, with divergence derivable for the per-hash colors.

The regression this fixed: `@{upstream}` is unresolvable from a detached
HEAD, so remote_hash came back None whenever the read tree sat detached.
Semantics (agreed on the issue):

  local_hash     short hash of the checked-out branch's TIP (stable across
                 the read tree's state)
  remote_hash    short hash of that branch's @{upstream} - resolved from
                 the BRANCH, so a detached tree no longer hides it

The direct world (#361) removed the worktree hash trio: there is ONE tree,
its HEAD is the branch tip, and dirty/ahead-behind describe it directly.
Remote chats (#333) keep parity: one multi-ref rev-parse burst through the
gateway fills both hashes.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.agent import gitinfo
from backend.agent.gitinfo import invalidate_git_caches


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _short(repo, ref):
    return _git(repo, "rev-parse", "--short=7", ref)


def _repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _commit(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name)


@pytest.fixture()
def fresh(tmp_path):
    yield tmp_path
    invalidate_git_caches(str(tmp_path / "repo"))


def _info(root, **kw):
    invalidate_git_caches(str(root))
    return gitinfo.git_workspace_info(root, **kw)


# ---- local path: the branch-tip pair ----


@pytest.mark.asyncio
async def test_local_hash_reports_the_checked_out_commit(fresh):
    """local_hash is the tip of the tree's own checked-out branch."""
    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    _commit(repo, "b.txt", "2")

    info = await _info(repo)

    assert info is not None
    assert info["local_hash"] == _short(repo, "master")
    assert info["remote_hash"] is None  # no upstream configured
    # The direct world (#361): the worktree hash trio is gone.
    assert "worktree_hash" not in info
    assert "worktree_ahead" not in info


@pytest.mark.asyncio
async def test_detached_tree_reports_its_own_commit(fresh):
    """A detached read tree reports the commit it sits on - there is no
    branch name to name, and the direct world fabricates none."""
    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    _commit(repo, "b.txt", "2")
    _git(repo, "checkout", "--quiet", "--detach", "HEAD~1")

    info = await _info(repo)

    assert info is not None
    assert info["local_hash"] == _short(repo, "HEAD")


@pytest.mark.asyncio
async def test_upstream_hash_resolved_from_the_branch(fresh):
    """remote_hash resolves from the branch, not from HEAD - the original
    #350 fix, which detachment used to defeat."""
    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    bare = fresh / "origin.git"
    _git(repo, "clone", "--bare", "--quiet", str(repo), str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "fetch", "--quiet", "origin")
    _git(repo, "branch", "--set-upstream-to=origin/master", "master")
    _commit(repo, "b.txt", "2")  # local ahead of upstream by 1

    info = await _info(repo)

    assert info is not None
    assert info["upstream"] == "origin/master"
    assert info["remote_hash"] == _short(bare, "master")
    assert info["local_hash"] == _short(repo, "master")
    assert info["ahead"] == 1 and info["behind"] == 0


# ---- remote parity (#333): hashes through the gateway ----


class _FakeHostSession:
    host_id = "h-fake"

    def __init__(self, host_repo):
        self.host_repo = str(host_repo)

    async def exec_tool(self, name, args, workspace=""):
        assert name == "bash", f"gateway must only ship bash, got {name}"
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


@pytest.mark.asyncio
async def test_remote_info_fills_branch_tip_and_upstream_hashes(
    fresh, _clear_sessions
):
    from backend.agent.remote import ns_path, register_remote

    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    bare = fresh / "origin.git"
    _git(repo, "clone", "--bare", "--quiet", str(repo), str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "fetch", "--quiet", "origin")
    _git(repo, "branch", "--set-upstream-to=origin/master", "master")
    _commit(repo, "b.txt", "2")
    register_remote(_FakeHostSession(repo))

    info = await _info(ns_path("h-fake", str(repo)))

    assert info is not None and not info.get("offline")
    assert info["local_hash"] == _short(repo, "master")
    assert info["remote_hash"] == _short(bare, "master")
    assert info["ahead"] == 1


@pytest.mark.asyncio
async def test_remote_info_hash_without_upstream(fresh, _clear_sessions):
    from backend.agent.remote import ns_path, register_remote

    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    register_remote(_FakeHostSession(repo))

    info = await _info(ns_path("h-fake", str(repo)))

    assert info is not None
    assert info["local_hash"] == _short(repo, "master")
    assert info["remote_hash"] is None


@pytest.mark.asyncio
async def test_remote_info_reports_detached_head_commit(
    fresh, _clear_sessions
):
    """Porcelain cannot name a detached HEAD; the readout falls back to the
    short SHA of the commit the tree sits on."""
    from backend.agent.remote import ns_path, register_remote

    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    _commit(repo, "b.txt", "2")
    _git(repo, "checkout", "--quiet", "--detach", "HEAD~1")
    register_remote(_FakeHostSession(repo))

    info = await _info(ns_path("h-fake", str(repo)))

    assert info is not None
    assert info["local_hash"] == _short(repo, "HEAD")


@pytest.mark.asyncio
async def test_remote_offline_stays_explicit(fresh, _clear_sessions):
    from backend.agent.remote import ns_path, register_remote

    class _Offline:
        host_id = "h-off"

        async def exec_tool(self, name, args, workspace=""):
            return {"error": "remote host unreachable: boom"}

    register_remote(_Offline())
    info = await _info(ns_path("h-off", "C:/repo"))
    assert info == {"offline": True}