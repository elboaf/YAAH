"""#321: the harness keeps ``<workspace>/.scratch/`` git-invisible.

Nothing else in the harness guarantees the deterministic per-chat
namespace stays out of ``git status`` — YAAH's own repo only works
because its ``.gitignore`` happens to carry ``.scratch/``. In a repo
that does not ignore it, agents see untracked noise and rationally
relocate worktrees, which silently breaks ``wt_sweep``/``runwatch``
(they find worktrees only under ``.scratch/``). The fix: before first
materialization, ``ensure_chat_worktree`` appends ``.scratch/`` to the
UNTRACKED ``.git/info/exclude`` (never the tracked ``.gitignore``),
idempotently, best-effort.
"""
import os
import subprocess

import pytest

from backend.agent import worktrees


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _repo_with_commit(tmp_path, name="repo", branch="master"):
    """A clean git repo with one commit on `branch` and a configured identity."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


def _exclude(repo):
    path = repo / ".git" / "info" / "exclude"
    return path.read_text(encoding="utf-8") if path.exists() else None


@pytest.mark.asyncio
async def test_appends_exclude_before_first_materialization(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    out = await worktrees.ensure_chat_worktree(str(repo), 31, "feature")
    assert out["created"] is True
    text = _exclude(repo) or ""
    assert ".scratch/" in text  # the rule is there (git templates the file)
    # And the namespace is really invisible: no untracked entries at all.
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_idempotent_second_call_writes_one_rule(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    await worktrees.ensure_chat_worktree(str(repo), 32, "feature")
    await worktrees.ensure_chat_worktree(str(repo), 32, "feature")
    text = _exclude(repo)
    assert text is not None
    assert text.count(".scratch/") == 1


@pytest.mark.asyncio
async def test_tracked_gitignore_covering_scratch_writes_nothing(tmp_path):
    repo = _repo_with_commit(tmp_path)
    (repo / ".gitignore").write_text(".scratch/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "gitignore")
    _git(repo, "branch", "feature")
    out = await worktrees.ensure_chat_worktree(str(repo), 33, "feature")
    assert out["created"] is True
    # Already ignored: no write at all (git templates the file with
    # comments, so presence isn't the signal - the rule's absence is).
    assert ".scratch/" not in (_exclude(repo) or "")


@pytest.mark.asyncio
async def test_existing_exclude_line_is_preserved(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    info = repo / ".git" / "info"
    info.mkdir(exist_ok=True)
    original = "# my local excludes\n*.log\n"
    (info / "exclude").write_text(original, encoding="utf-8")
    await worktrees.ensure_chat_worktree(str(repo), 34, "feature")
    text = _exclude(repo)
    assert text.startswith(original)  # user content untouched, prepended
    assert text.count(".scratch/") == 1


@pytest.mark.asyncio
async def test_unwritable_exclude_degrades_without_blocking(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    info = repo / ".git" / "info"
    info.mkdir(exist_ok=True)
    # A DIRECTORY where info/exclude belongs: every read/write probe
    # fails with an OSError, so the guard must log-and-continue.
    (info / "exclude").unlink()  # git templates a file here
    (info / "exclude").mkdir()
    out = await worktrees.ensure_chat_worktree(str(repo), 35, "feature")
    assert out["created"] is True  # the run is never blocked
    assert (repo / ".scratch" / "chat-35").exists()


@pytest.mark.asyncio
async def test_existing_chat_dir_also_establishes_invisibility(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "feature")
    (repo / ".scratch" / "chat-36").mkdir(parents=True)
    out = await worktrees.ensure_chat_worktree(str(repo), 36, "feature")
    assert out["created"] is False  # the worktree-exists early path
    assert ".scratch/" in (_exclude(repo) or "")


@pytest.mark.asyncio
async def test_non_git_and_remote_workspaces_are_noops(tmp_path, caplog):
    # A non-repo directory: the guard must not create .git or anything.
    plain = tmp_path / "plain"
    plain.mkdir()
    await worktrees._ensure_scratch_invisible(str(plain))
    assert not (plain / ".git").exists()
    # Remote namespace: nothing to do, never touches the local disk.
    await worktrees._ensure_scratch_invisible("remote:host:/srv/repo")
    await worktrees._ensure_scratch_invisible(None)
    await worktrees._ensure_scratch_invisible(".")
