"""Issue #331: a workspace the user `git init`s AFTER registering it (an
unborn HEAD - repo exists, zero commits) must be recognized as a git repo.

Before the fix, `current_git_branch` identified the branch with
`git rev-parse --abbrev-ref HEAD`, which fails (rc 128) on an unborn
HEAD - so every surface conflated "freshly init'ed repo" with "not a
repo": no status-strip readout, empty branch lists, no #301 pin, no #314
picker, and chat-worktree creation failed outright. Detection is
stateless (a restart never fixed it); only the first commit did.
"""
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.agent import gitinfo
from backend.agent import worktrees
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


# ---- #301 pin at creation ----


@pytest.mark.asyncio
async def test_pin_at_creation_on_unborn_repo(db, tmp_path):
    """A chat created against a freshly init'ed workspace pins its unborn
    branch as inherited - the workspace is a repo, and the pin's name is
    exactly what the first commit will land on."""
    repo = _unborn(tmp_path)
    cid = await db_mod.create_conversation("t", str(repo))
    conv = await db_mod.get_conversation(cid)
    assert conv["selected_branch"] == "master"
    assert conv["branch_pin_origin"] == "inherited"


@pytest.mark.asyncio
async def test_stale_pin_check_not_triggered_by_unborn(db, tmp_path):
    """The run-path staleness guard reads the same branch list: a pin on
    the unborn branch is not 'deleted upstream'."""
    repo = _unborn(tmp_path)
    cid = await db_mod.create_conversation("t", str(repo))
    conv = await db_mod.get_conversation(cid)
    from backend.agent.gitinfo import is_git_repo, list_local_branches
    from backend.agent.tools import workspace_root

    root = workspace_root(conv["workspace"])
    assert is_git_repo(root) is True
    assert conv["selected_branch"] in await list_local_branches(root)


# ---- chat worktree from an unborn HEAD (#277 materialization) ----


def _wt_head_symref(chat_dir: Path) -> str:
    head = (chat_dir / ".git").read_text(encoding="utf-8")
    target = head.strip().removeprefix("gitdir:").strip()
    gitdir = Path(target)
    if not gitdir.is_absolute():
        gitdir = (chat_dir / gitdir).resolve()
    text = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    return text.removeprefix("ref: refs/heads/")


@pytest.mark.asyncio
async def test_worktree_from_unborn_pins_the_pinned_name(tmp_path):
    """Plain `worktree add <dir> <branch>` fails on an unborn HEAD
    ('invalid reference'); the deliberate path is --orphan -b <branch>,
    so the orphan branch is the PINNED name - never the directory name
    the bare form silently invents."""
    repo = _unborn(tmp_path)
    out = await worktrees.ensure_chat_worktree(str(repo), 33, "master")
    assert out.get("error") is None, out
    assert out["created"] is True
    assert out["detached"] is False
    assert out["path"] == repo / ".scratch" / "chat-33"
    assert _wt_head_symref(out["path"]) == "master"


@pytest.mark.asyncio
async def test_worktree_from_unborn_new_branch_name(tmp_path):
    """A pin on a branch that does not exist anywhere yet (fresh init, the
    user picked a new name): the orphan worktree carries that name."""
    repo = _unborn(tmp_path)
    out = await worktrees.ensure_chat_worktree(str(repo), 34, "feature")
    assert out.get("error") is None, out
    assert _wt_head_symref(out["path"]) == "feature"


@pytest.mark.asyncio
async def test_unborn_worktree_flips_cleanly_after_first_commit(tmp_path):
    """The shared-unborn-branch case: primary and chat worktree both hold
    the unborn branch; the first commit in the primary lifts both HEADs
    onto it, so a later run's existing-tree checkout is a normal flip."""
    repo = _unborn(tmp_path)
    out = await worktrees.ensure_chat_worktree(str(repo), 35, "master")
    assert out.get("error") is None, out
    chat_dir = out["path"]
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    second = await worktrees.ensure_chat_worktree(str(repo), 35, "master")
    assert second.get("error") is None, second
    assert second["created"] is False
    assert second["detached"] is False
    tip = _git(repo, "rev-parse", "master")
    wtip = _git(chat_dir, "rev-parse", "HEAD")
    assert tip == wtip


@pytest.mark.asyncio
async def test_born_repo_free_branch_never_gets_an_orphan(tmp_path):
    """The gate that keeps the orphan path honest (#331 review): on a born
    repo, a plain add fails for a REAL reason even when the branch does
    not exist yet ('invalid reference'). Git would happily accept
    --orphan -b there and hand back an empty history-less worktree - the
    fallback must not fire, and nothing may be left behind."""
    repo = _born(tmp_path)
    out = await worktrees.ensure_chat_worktree(str(repo), 37, "brand-new")
    assert out["path"] is None
    assert "invalid reference" in out["error"]
    # No spurious branch, no residue worktree.
    assert "brand-new" not in await gitinfo.list_local_branches(repo)
    _git(repo, "worktree", "prune")


@pytest.mark.asyncio
async def test_unborn_old_git_surfaces_the_specified_clear_error(tmp_path, monkeypatch):
    """Criterion 3: where the orphan form is unavailable (old git), the
    error names the actual situation - 'no commits yet' - instead of a
    generic worktree failure."""
    repo = _unborn(tmp_path)
    real_run_git = worktrees._run_git

    async def old_git(root, *args, **kwargs):
        if "--orphan" in args:
            return 128, "fatal: unknown option: orphan"
        return await real_run_git(root, *args, **kwargs)

    monkeypatch.setattr(worktrees, "_run_git", old_git)
    out = await worktrees.ensure_chat_worktree(str(repo), 38, "master")
    assert out["path"] is None
    assert out["error"].startswith("workspace repo has no commits yet")


@pytest.mark.asyncio
async def test_born_repo_worktree_path_unchanged(tmp_path):
    """Guard: the orphan path must not fire for a normal repo."""
    repo = _born(tmp_path)
    _git(repo, "branch", "feature")
    out = await worktrees.ensure_chat_worktree(str(repo), 36, "feature")
    assert out.get("error") is None, out
    assert out["created"] is True
    assert _wt_head_symref(out["path"]) == "feature"
