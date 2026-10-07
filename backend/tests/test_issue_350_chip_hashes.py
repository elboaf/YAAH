"""Issue #350: the branch chip's sync readout must show three commit
hashes - the selected branch's tip, its upstream's tip, and the chat's own
worktree HEAD - with divergence derivable for the per-hash colors.

The regression this fixes: the chip reads the chat worktree (#277), which
usually sits DETACHED at the branch tip, and `@{upstream}` is unresolvable
from a detached HEAD - so remote_hash came back None and the second hash
vanished whenever a chat had materialized its tree. Semantics (agreed on
the issue):

  local_hash     short hash of the SELECTED BRANCH TIP (stable across
                 checkouts/detachments - no longer the read tree's HEAD)
  remote_hash    short hash of that branch's @{upstream} - resolved from
                 the BRANCH, so detachment no longer hides it
  worktree_hash  HEAD of the chat's own worktree; None when the chat has
                 no tree (omitted in the UI, never zero-filled)
  worktree_ahead       commits the chat tree has that the primary tree lacks
                 (diverged counts as ahead; unrelated histories -> 0)

Remote chats (#333) get parity: one extra multi-ref rev-parse burst
through the gateway fills local/remote hashes; worktree_hash stays a
local-only concept (the host-side chat-tree substitution is #334).
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


# ---- local path: three hashes with distinct semantics ----


@pytest.mark.asyncio
async def test_local_hash_is_branch_tip_even_when_tree_detached(fresh):
    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    _commit(repo, "b.txt", "2")
    # The #277 common case: the chat's tree is detached at the FIRST commit
    # while master already moved on.
    chat = fresh / "chat"
    _git(repo, "worktree", "add", "--detach", str(chat), "HEAD~1")

    info = await _info(repo, chat_root=chat, branch="master")

    assert info is not None
    # local = the branch TIP, not the (detached) read-tree HEAD...
    assert info["local_hash"] == _short(repo, "master")
    # ...which is exactly what keeps upstream resolvable from the branch.
    assert info["remote_hash"] is None  # no upstream configured
    # worktree = where the chat's tree actually sits.
    assert info["worktree_hash"] == _short(repo, "HEAD~1")


@pytest.mark.asyncio
async def test_upstream_hash_survives_detached_chat_tree(fresh):
    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    bare = fresh / "origin.git"
    _git(repo, "clone", "--bare", "--quiet", str(repo), str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "fetch", "--quiet", "origin")
    _git(repo, "branch", "--set-upstream-to=origin/master", "master")
    _commit(repo, "b.txt", "2")  # local ahead of upstream by 1
    chat = fresh / "chat"
    _git(repo, "worktree", "add", "--detach", str(chat), "HEAD~1")

    info = await _info(repo, chat_root=chat, branch="master")

    assert info is not None
    assert info["upstream"] == "origin/master"
    assert info["remote_hash"] == _short(bare, "master")
    assert info["local_hash"] == _short(repo, "master")
    assert info["ahead"] == 1 and info["behind"] == 0


@pytest.mark.asyncio
async def test_worktree_hash_absent_without_chat_tree(fresh):
    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")

    info = await _info(repo)

    assert info is not None
    assert info["worktree_hash"] is None
    assert info["worktree_ahead"] == 0
    assert info["local_hash"] == _short(repo, "master")


@pytest.mark.asyncio
async def test_worktree_ahead_counts_commits_not_in_primary(fresh):
    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    chat = fresh / "chat"
    _git(repo, "worktree", "add", "--detach", str(chat), "HEAD")
    _commit(chat, "wip.txt", "run work")  # a commit only the chat tree has

    info = await _info(repo, chat_root=chat, branch="master")

    assert info is not None
    assert info["worktree_hash"] == _short(chat, "HEAD")
    assert info["worktree_ahead"] == 1


@pytest.mark.asyncio
async def test_worktree_ahead_diverged_still_counts_as_ahead(fresh):
    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    chat = fresh / "chat"
    _git(repo, "worktree", "add", "--detach", str(chat), "HEAD")
    _commit(chat, "wip.txt", "run work")     # chat tree diverges...
    _commit(repo, "main.txt", "human work")  # ...while master moved on

    info = await _info(repo, chat_root=chat, branch="master")

    assert info is not None
    assert info["worktree_ahead"] == 1


@pytest.mark.asyncio
async def test_worktree_ahead_zero_for_unrelated_histories(fresh):
    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    _git(repo, "checkout", "--orphan", "side")
    _git(repo, "rm", "-rf", "--quiet", ".")
    _commit(repo, "other.txt", "orphan root")
    _git(repo, "checkout", "--quiet", "master")
    chat = fresh / "chat"
    _git(repo, "worktree", "add", "--detach", str(chat), "side")

    info = await _info(repo, chat_root=chat, branch="master")

    assert info is not None
    assert info["worktree_hash"] == _short(chat, "HEAD")
    # rev-list cannot count unrelated histories (rc 128) -> never claim a
    # direction the count cannot back.
    assert info["worktree_ahead"] == 0


@pytest.mark.asyncio
async def test_info_cache_does_not_bleed_across_chats(fresh):
    """#350 review: the info cache is keyed per chat (root + chat tree +
    branch), not per workspace - two chats sharing a workspace must never
    serve each other's worktree hash within the TTL."""
    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    chat_a = fresh / "chatA"
    chat_b = fresh / "chatB"
    _git(repo, "worktree", "add", "--detach", str(chat_a), "HEAD")
    _git(repo, "worktree", "add", "--detach", str(chat_b), "HEAD")
    _commit(chat_a, "wip.txt", "a work")  # a commit only chat A's tree has

    info_a = await _info(repo, chat_root=chat_a, branch="master")
    info_b = await _info(repo, chat_root=chat_b, branch="master")

    assert info_a["worktree_hash"] == _short(chat_a, "HEAD")
    # A shared cache entry would leak A's tree state into B's readout.
    assert info_b["worktree_hash"] == _short(chat_b, "HEAD")
    assert info_b["worktree_hash"] != info_a["worktree_hash"]
    assert info_b["worktree_ahead"] == 0


@pytest.mark.asyncio
async def test_dirty_describes_chat_tree_when_it_exists(fresh):
    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    chat = fresh / "chat"
    _git(repo, "worktree", "add", "--detach", str(chat), "HEAD")
    (chat / "dirty.txt").write_text("x\n", encoding="utf-8")

    info = await _info(repo, chat_root=chat, branch="master")

    assert info is not None
    assert info["dirty"] is True
    assert info["untracked"] == 1


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

    info = await _info(ns_path("h-fake", str(repo)), branch="master")

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

    info = await _info(ns_path("h-fake", str(repo)), branch="master")

    assert info is not None
    assert info["local_hash"] == _short(repo, "master")
    assert info["remote_hash"] is None


@pytest.mark.asyncio
async def test_remote_info_local_hash_uses_branch_not_detached_head(
    fresh, _clear_sessions
):
    from backend.agent.remote import ns_path, register_remote

    repo = _repo(fresh)
    _commit(repo, "a.txt", "1")
    _commit(repo, "b.txt", "2")
    _git(repo, "checkout", "--quiet", "--detach", "HEAD~1")
    register_remote(_FakeHostSession(repo))

    info = await _info(ns_path("h-fake", str(repo)), branch="master")

    assert info is not None
    assert info["local_hash"] == _short(repo, "master")
    assert info["local_hash"] != _short(repo, "HEAD")


@pytest.mark.asyncio
async def test_remote_offline_stays_explicit(fresh, _clear_sessions):
    from backend.agent.remote import ns_path, register_remote

    class _Offline:
        host_id = "h-off"

        async def exec_tool(self, name, args, workspace=""):
            return {"error": "remote host unreachable: boom"}

    register_remote(_Offline())
    info = await _info(ns_path("h-off", "C:/repo"), branch="master")
    assert info == {"offline": True}
