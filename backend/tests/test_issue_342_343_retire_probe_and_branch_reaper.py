"""#342 + #343: local chat-worktree retirement must not be defeated by the
empty `.scratch` husk git leaves behind after the agent removes the run
worktree per the SOP, and a landed run branch's ref must be reaped
safemode (`-d`, never `-D`) instead of living forever.

The #342 half: `chat_worktree_has_run_tree` pinned on directory
EXISTENCE (per #330) because the run namespace is git-invisible (#321).
That is correct for `<chat>/run` (a standing run worktree) but wrong for
the empty `<chat>/.scratch/chat-<id>/` husk `git worktree remove
<chat>/run` leaves behind forever: the husk is not state, and reading
it as "run residue present" made retirement refuse on EVERY clean
SOP-landed chat. The probe now keys the residue verdict on the git
worktree REGISTRY (a registered worktree under the chat tree), while
the husk is pruned best-effort after a successful retirement.

The #343 half: every run mints `run/<slug>-<chat-id>` (#312); nothing
ever deleted the ref. Retirement now reaps it with `branch -d` (safe
mode: refuses when unmerged), never `-D`, and only the chat's own
deterministic branch. The sweeper's sweep result grows `reaped_branches`
for the same verdict on dead-chat trees it removes.
"""
import os
import subprocess
import time


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
    # The primary tree works on its own branch, leaving the chat's
    # selected branch (master) free for the chat worktree to attach —
    # the ADR-0010 arrangement this lifecycle is built on.
    _git(repo, "checkout", "-q", "-b", "dev")
    return repo


async def _materialize(repo, cid=42):
    from backend.agent.worktrees import ensure_chat_worktree

    return await ensure_chat_worktree(str(repo), cid, "master")


def _follow_run_sop(repo, chat_dir, work="landed change\n", chat_id=42):
    """The agent SOP, executed literally: the run worktree stands at the
    deterministic nested path (``<chat>/.scratch/chat-<id>/run``, git
    creates the intermediate dirs), the agent branches and lands there,
    then removes it — leaving the empty ``<chat>/.scratch/chat-<id>/``
    husk, the exact state #342 is about."""
    cid = str(chat_id)
    branch = f"run/fix-something-{cid}"
    run_dir = chat_dir / ".scratch" / f"chat-{cid}" / "run"
    # The chat tree holds the selected branch, so the add detaches —
    # precisely what ensure_chat_worktree's fallback does for the agent.
    _git(chat_dir, "worktree", "add", "--detach", str(run_dir), "HEAD")
    _git(run_dir, "checkout", "-q", "-b", branch)
    _git(run_dir, "config", "user.email", "test@example.com")
    _git(run_dir, "config", "user.name", "Test")
    (run_dir / "work.txt").write_text(work, encoding="utf-8")
    _git(run_dir, "add", "-A")
    _git(run_dir, "commit", "-q", "-m", "do the thing")
    _git(chat_dir, "merge", "--no-ff", "-q", "-m", f"Landing {branch}", branch)
    _git(chat_dir, "worktree", "remove", str(run_dir))


# --------------------------------------------------------------- #342

async def test_retire_succeeds_after_a_clean_sop_landing(tmp_path):
    """THE #342 bug, end to end: work landed clean per the SOP, run
    worktree removed — only the empty `.scratch` husk remains. Retirement
    must retire (the old probe refused forever with 'run residue
    present'), and must prune the husk on its way out."""
    from backend.agent.worktrees import retire_chat_worktree

    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]
    _follow_run_sop(repo, chat_dir)

    assert (chat_dir / ".scratch" / "chat-42").exists()  # the husk
    result = await retire_chat_worktree(str(repo), 42)
    assert result["retired"] is True, result
    assert not chat_dir.exists()


async def test_a_standing_run_worktree_still_pins_the_tree(tmp_path):
    """The registry probe still recognizes REAL residue: a registered
    run worktree under the chat tree keeps retirement off."""
    from backend.agent.worktrees import retire_chat_worktree

    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]
    # A standing (registered) run worktree, as during an in-flight
    # run: the harness nests it at <chat>/.scratch/chat-<id>/run and
    # it stays registered until the agent's SOP removes it.
    nested = chat_dir / ".scratch" / "chat-42" / "run"
    _git(chat_dir, "worktree", "add", "--detach", str(nested), "HEAD")
    (nested / "wip.txt").write_text("x", encoding="utf-8")

    result = await retire_chat_worktree(str(repo), 42)
    assert result["retired"] is False
    assert result["reason"] == "run residue present"
    assert chat_dir.exists()


async def test_husk_probe_is_registry_based_not_existence(tmp_path):
    """Unit shape of the #342 fix: with ONLY the empty husk present, the
    probe says False; with a registered run worktree present, True."""
    from backend.agent.worktrees import chat_worktree_has_run_tree

    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]
    _follow_run_sop(repo, chat_dir)

    assert (chat_dir / ".scratch").exists()
    assert await chat_worktree_has_run_tree(chat_dir) is False

    # And the in-flight case still reads True: a REGISTERED nested
    # run worktree (not merely dirs on disk).
    nested = chat_dir / ".scratch" / "chat-42" / "run"
    _git(chat_dir, "worktree", "add", "--detach", str(nested), "HEAD")
    assert await chat_worktree_has_run_tree(chat_dir) is True


# --------------------------------------------------------------- #343

async def test_retire_reaps_the_landed_run_branch_safemode(tmp_path):
    """After a landed-clean SOP retirement, the run branch ref is gone —
    and the removal went through `branch -d` (safe mode), so an
    unmerged branch could never take this path."""
    from backend.agent.worktrees import retire_chat_worktree

    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]
    _follow_run_sop(repo, chat_dir)
    assert "run/fix-something-42" in _git(repo, "branch", "--list", "run/fix-something-42")

    result = await retire_chat_worktree(str(repo), 42)
    assert result["retired"] is True, result
    assert result.get("reaped_branches") == ["run/fix-something-42"]
    assert "run/fix-something-42" not in _git(repo, "branch", "--list", "all")


async def test_retire_keeps_branch_of_dirty_tree(tmp_path):
    """Never reap on a refusal: a dirty tree survives WITH its run
    branch ref — the residue protocol owns that surface."""
    from backend.agent.worktrees import retire_chat_worktree

    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]
    _follow_run_sop(repo, chat_dir)
    (chat_dir / "late-change.txt").write_text("wip", encoding="utf-8")

    result = await retire_chat_worktree(str(repo), 42)
    assert result["retired"] is False
    assert "run/fix-something-42" in _git(repo, "branch", "--list", "run/fix-something-42")


async def test_unmerged_run_branch_is_never_reaped(tmp_path):
    """Safe-mode guarantee: even when the branch name is discovered, a
    branch whose commits are NOT reachable from any local ref tip the
    merge-check accepts stays alive. Simulate by planting the name
    without a merge — retirement (dirty) refuses; and the reaper's
    `branch -d` alone refuses unmerged refs."""
    from backend.agent.worktrees import reap_merged_run_branches

    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]
    _git(chat_dir, "config", "user.email", "test@example.com")
    _git(chat_dir, "config", "user.name", "Test")
    _git(chat_dir, "checkout", "-q", "-b", "run/unmerged-42")
    (chat_dir / "work.txt").write_text("precious\n", encoding="utf-8")
    _git(chat_dir, "add", "-A")
    _git(chat_dir, "commit", "-q", "-m", "not landed anywhere")
    _git(chat_dir, "checkout", "-q", "master")

    result = await reap_merged_run_branches(
        str(repo), ["run/unmerged-42"], "master"
    )
    assert result == []  # refused, safe mode
    assert "run/unmerged-42" in _git(repo, "branch", "--list", "run/unmerged-42")


async def test_retire_without_a_run_branch_retires_clean(tmp_path):
    """A chat that never minted a run branch retires normally; the reap
    is a silent no-op (no phantom entries)."""
    from backend.agent.worktrees import retire_chat_worktree

    repo = _repo_with_commit(tmp_path)
    out = await _materialize(repo)
    chat_dir = out["path"]

    result = await retire_chat_worktree(str(repo), 42)
    assert result["retired"] is True, result
    assert result.get("reaped_branches") == []


# ------------------------------------------- #343: the sweeper's share

async def _register_workspace(repo):
    from backend.db.database import get_db

    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO workspaces (path, label) VALUES (?, ?)", (str(repo), "r")
        )
        await db.commit()
    finally:
        await db.close()


def _age(tree, days=40):
    old = time.time() - days * 24 * 3600
    os.utime(tree, (old, old))


async def test_sweep_reaps_landed_run_branches_of_dead_chats(tmp_path):
    """A dead chat's clean, aged, SOP-landed tree is swept AND its run
    branch ref is reaped in the same pass."""
    from backend.agent import wt_sweep, worktrees

    repo = _repo_with_commit(tmp_path)
    await _register_workspace(repo)
    out = await worktrees.ensure_chat_worktree(str(repo), 867, "master")
    chat_dir = out["path"]
    _follow_run_sop(repo, chat_dir, chat_id=867)
    _age(chat_dir)

    result = await wt_sweep.sweep_stale_chat_worktrees()
    assert str(chat_dir) in result["swept"]
    assert result["reaped_branches"] == ["run/fix-something-867"]
    assert "run/fix-something-867" not in _git(repo, "branch", "--list", "run/fix-something-867")
    assert not chat_dir.exists()


async def test_sweep_never_reaps_a_branch_the_tree_keeps(tmp_path):
    """A residue (in-flight) tree is kept by the sweeper — its run
    branch ref must survive untouched."""
    from backend.agent import wt_sweep, worktrees

    repo = _repo_with_commit(tmp_path)
    await _register_workspace(repo)
    out = await worktrees.ensure_chat_worktree(str(repo), 869, "master")
    chat_dir = out["path"]
    _git(chat_dir, "config", "user.email", "test@example.com")
    _git(chat_dir, "config", "user.name", "Test")
    _git(chat_dir, "checkout", "-q", "-b", "run/in-flight-869")
    (chat_dir / "wip.txt").write_text("x", encoding="utf-8")
    _git(chat_dir, "add", "-A")
    _git(chat_dir, "commit", "-q", "-m", "wip")
    _git(chat_dir, "checkout", "-q", "master")
    nested = await worktrees.ensure_chat_worktree(str(chat_dir), 869, "master")
    assert nested["path"] is not None
    _age(chat_dir)

    result = await wt_sweep.sweep_stale_chat_worktrees()
    assert str(chat_dir) not in result["swept"]
    assert result["reaped_branches"] == []
    assert "run/in-flight-869" in _git(repo, "branch", "--list", "run/in-flight-869")
