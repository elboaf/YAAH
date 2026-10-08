"""Issue #331: a workspace the user `git init`s AFTER registering it (an
unborn HEAD - repo exists, zero commits) must be recognized as a git repo.

Before the fix, `current_git_branch` identified the branch with
`git rev-parse --abbrev-ref HEAD`, which fails (rc 128) on an unborn
HEAD - so every surface conflated "freshly init'ed repo" with "not a
repo": no status-strip readout and empty branch lists. Detection is
stateless (a restart never fixed it); only the first commit did.
"""
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.agent import gitinfo
from backend.db import database as db_mod


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _unborn(tmp_path, name="repo", branch="master"):
    """A repo the way #331 found it: `git init` only - no commits, so HEAD
    points at a ref that does not exist yet."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    return repo


def _born(tmp_path, name="repo", branch="master"):
    """Same repo after the first commit."""
    repo = _unborn(tmp_path, name, branch)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A throwaway database, schema included."""
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "agent.db")
    return db_mod


@pytest.fixture(autouse=True)
def _fresh_caches():
    """The module-level TTL caches must not leak state between tests."""
    gitinfo._cache.clear()
    gitinfo._info_cache.clear()
    gitinfo._branch_list_cache.clear()
    yield
    gitinfo._cache.clear()
    gitinfo._info_cache.clear()
    gitinfo._branch_list_cache.clear()


# ---- detection ----


@pytest.mark.asyncio
async def test_current_git_branch_reports_unborn_branch(tmp_path):
    repo = _unborn(tmp_path)
    assert await gitinfo.current_git_branch(repo) == "master"


@pytest.mark.asyncio
async def test_head_branch_reports_unborn_symref(tmp_path):
    repo = _unborn(tmp_path, branch="trunk")
    assert await gitinfo.head_branch(repo) == "trunk"


@pytest.mark.asyncio
async def test_head_branch_reports_born_branch_too(tmp_path):
    """head_branch reads the symref for any attached HEAD - born included.
    Callers gate it (rev-parse first, empty branch list first), so the
    broader read is unreachable on born repos; the contract is documented
    here so nobody 'tightens' it into an unborn-only probe."""
    repo = _born(tmp_path)
    assert await gitinfo.head_branch(repo) == "master"


@pytest.mark.asyncio
async def test_head_branch_none_for_detached_head(tmp_path):
    repo = _born(tmp_path)
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    assert await gitinfo.head_branch(repo) is None


@pytest.mark.asyncio
async def test_is_git_repo_true_for_unborn(tmp_path):
    repo = _unborn(tmp_path)
    assert gitinfo.is_git_repo(repo) is True


@pytest.mark.asyncio
async def test_non_repo_still_reports_nothing(tmp_path):
    """The fallback must not over-reach: a plain folder stays a non-repo."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert await gitinfo.current_git_branch(plain) is None
    assert await gitinfo.head_branch(plain) is None
    assert await gitinfo.list_local_branches(plain) == []


# ---- branch list / status readout ----


@pytest.mark.asyncio
async def test_list_local_branches_includes_unborn(tmp_path):
    repo = _unborn(tmp_path, branch="main")
    assert await gitinfo.list_local_branches(repo) == ["main"]


@pytest.mark.asyncio
async def test_list_local_branches_born_still_full(tmp_path):
    repo = _born(tmp_path)
    _git(repo, "branch", "feature")
    assert await gitinfo.list_local_branches(repo) == ["feature", "master"]


@pytest.mark.asyncio
async def test_workspace_info_reports_branch_without_hash(tmp_path):
    """The strip shows the branch; there is no commit hash to show yet."""
    repo = _unborn(tmp_path)
    info = await gitinfo.git_workspace_info(repo)
    assert info is not None
    assert info["branch"] == "master"
    assert info["local_hash"] is None
    assert info["remote_hash"] is None
    assert info["dirty"] is False
    assert info["ahead"] == 0 and info["behind"] == 0


@pytest.mark.asyncio
async def test_legacy_negative_poll_recovers_at_first_commit(tmp_path):
    """The broken build cached branch=None keyed on HEAD's mtime - and the
    first commit does not touch .git/HEAD, so a workspace stuck at None
    stayed stuck until restart. Whatever a poll stored while unborn, the
    repo must read as healthy once commits exist."""
    repo = _unborn(tmp_path)
    head = repo / ".git" / "HEAD"
    # Prime the cache the way the broken build did: mtime -> None. The
    # branch-list entry is primed EXPIRED: in production a stale None/[]
    # entry simply ages out of the 2s TTL within one poll cadence.
    gitinfo._cache[str(repo)] = (head.stat().st_mtime_ns, None)
    gitinfo._branch_list_cache[str(repo)] = (time.monotonic() - 60.0, [])
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    assert await gitinfo.current_git_branch(repo) == "master"
    assert "master" in await gitinfo.list_local_branches(repo)
