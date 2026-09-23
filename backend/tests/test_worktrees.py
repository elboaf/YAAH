"""Issue #58: worktree-per-writing-agent + mutex-serialized merge-back.

Covers the harness contract end to end on real temp git repos: rebinding
(ensure_isolated), merge-back refusal rules (dirty worktree, zero commits,
dirty overlap with the merge, conflict abort), self_merge lifecycle,
sub-agent result finalization (branch on the first line), the reaper's
salvage-before-delete, and the UI hygiene filters.
"""

import asyncio
import os
import time
from pathlib import Path

import pytest

from backend.agent import worktrees

from backend.tests.gitutil import run_git


def _git(cwd: Path, *args: str) -> str:
    return run_git(cwd, *args).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A real git repo with one commit (HEAD must exist to branch from)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "hello.txt").write_text("v1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture(autouse=True)
def _clean_state():
    """No session leakage between tests."""
    worktrees._active.clear()
    worktrees._chat_bindings.clear()
    worktrees._shared_writers.clear()
    yield
    worktrees._active.clear()
    worktrees._chat_bindings.clear()
    worktrees._shared_writers.clear()


# ---------------------------------------------------------------- unit bits


def test_branch_for_is_git_safe():
    b = worktrees.branch_for("chat/with:weird chars", "run id 42")
    assert b.startswith("agent/")
    assert " " not in b and ":" not in b
    assert b.count("/") == 2  # agent/<chat>/<run>


def test_branch_for_includes_human_label():
    """Readable branches: agent/<chat>/<title-slug>-<run-id>. The uuid
    suffix stays for anti-collision; the label makes the branch readable
    in a branch list (users were confused by bare agent/221/f983bfef)."""
    b = worktrees.branch_for("221", "f983bfef6b0f", label="Fix #93: scheduled ask_user")
    assert b == "agent/221/fix-93-scheduled-ask-user-f983bfef6b0f"


def test_branch_for_label_truncation_keeps_run_suffix():
    """A long title is trimmed — never the run uuid at the end."""
    b = worktrees.branch_for("7", "abc123def456", label="x" * 200)
    assert b.endswith("-abc123def456")
    assert b.count("/") == 2
    for part in b.split("/"):
        assert len(part) <= 60


def test_branch_for_without_label_unchanged():
    assert worktrees.branch_for("221", "f983bfef6b0f") == "agent/221/f983bfef6b0f"


def test_branch_for_label_that_slugs_to_nothing_falls_back():
    """A label of only stripped characters must not yield a dangling dash."""
    b = worktrees.branch_for("5", "abc123def456", label="###")
    assert b == "agent/5/abc123def456"


def test_worktree_of_is_pure_path_shape(tmp_path: Path):
    main = tmp_path / "proj"
    (main / ".yaah" / "worktrees" / "abc").mkdir(parents=True)
    (main / "src").mkdir()
    assert worktrees.worktree_of(str(main / ".yaah" / "worktrees" / "abc")) == str(
        (main / ".yaah" / "worktrees" / "abc").resolve()
    )
    assert worktrees.worktree_of(str(main / "src")) is None


def test_filter_agent_branches():
    assert worktrees.filter_agent_branches(
        ["master", "agent/chat1/abc", "feature", "agent/chat2/def"]
    ) == ["master", "feature"]


def test_exclude_uses_info_exclude_not_gitignore(repo: Path):
    worktrees._exclude_worktrees(repo)
    text = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".yaah/" in text.splitlines()
    assert not (repo / ".gitignore").exists()
    # idempotent
    worktrees._exclude_worktrees(repo)
    assert text.count(".yaah/") == 1 or ".yaah/" in text.splitlines()


# ------------------------------------------------------------- rebinding


async def test_ensure_isolated_creates_and_binds(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="1")
    try:
        assert wt != str(repo)
        assert worktrees.worktree_of(wt) == str(Path(wt))
        info = worktrees.binding_for("1")
        assert info and info["root"] == str(repo)
        assert info["branch"].startswith("agent/1/")
        # the worktree is real: HEAD works, and the base file is there
        assert (Path(wt) / "hello.txt").read_text(encoding="utf-8") == "v1\n"
        # second call for the same chat rebinds to the SAME worktree
        again = await worktrees.ensure_isolated(str(repo), chat_id="1")
        assert again == wt
    finally:
        await worktrees.self_merge("1")


async def test_ensure_isolated_branches_with_conversation_title(repo: Path, monkeypatch):
    """#branch-names: the top-level seam resolves a human label (the pinned
    conversation's title) so branches read agent/<chat>/<title-slug>-<run>."""
    async def fake_title(chat_id: str) -> str | None:
        return "Fix #93: scheduled ask_user"

    monkeypatch.setattr(worktrees, "_chat_title", fake_title)
    wt = await worktrees.ensure_isolated(str(repo), chat_id="99")
    try:
        info = worktrees.binding_for("99")
        assert info is not None
        branch = info["branch"]
        assert branch.startswith("agent/99/fix-93-scheduled-ask-user-")
        assert branch.count("/") == 2
    finally:
        await worktrees.self_merge("99")


async def test_ensure_isolated_nonrepo_first_writer_passes(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    ws = await worktrees.ensure_isolated(str(plain), chat_id="1")
    assert ws == str(plain)
    # a second concurrent writer is refused — never a silent shared tree
    with pytest.raises(worktrees.IsolationRefused):
        await worktrees.ensure_isolated(str(plain), chat_id="2")
    worktrees.release_chat("1")
    # after release, the next writer is fine again
    ws = await worktrees.ensure_isolated(str(plain), chat_id="3")
    assert ws == str(plain)


async def test_main_repo_root_through_a_worktree(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="1")
    try:
        # from INSIDE the worktree, the main root still resolves
        assert await worktrees.main_repo_root(wt) == repo
    finally:
        await worktrees.self_merge("1")


# ------------------------------------------------------------ merge-back


async def test_merge_back_refuses_zero_commits(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="1")
    info = worktrees.binding_for("1")
    result = await worktrees.merge_back(repo, info["branch"])
    assert result["merged"] is False
    assert "no commits" in result["reason"]
    # the worktree survives a refusal
    assert Path(wt).exists()


async def test_merge_back_lands_committed_work(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="1")
    info = worktrees.binding_for("1")
    (Path(wt) / "feature.txt").write_text("work\n", encoding="utf-8")
    _git(Path(wt), "add", "-A")
    _git(Path(wt), "commit", "-q", "-m", "feature")
    result = await worktrees.merge_back(repo, info["branch"])
    assert result["merged"] is True and result["commits"] == 1
    # the file is in the main tree; the branch is kept for inspection.
    # (merge_back is the merge primitive: worktree removal is the
    # self_merge/finalize lifecycle's job, covered in those tests.)
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "work\n"
    assert info["branch"] in _git(repo, "branch", "--list", info["branch"])


async def test_merge_back_merges_around_unrelated_dirty_main(repo: Path):
    """Revisit of issue #58 decision 1 (docs/adr/0001): dirt that does not
    collide with the merge must not veto it — git's own overlap-aware
    pre-flight is the gate."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="1")
    info = worktrees.binding_for("1")
    (Path(wt) / "f.txt").write_text("x\n", encoding="utf-8")
    _git(Path(wt), "add", "-A")
    _git(Path(wt), "commit", "-q", "-m", "x")
    # user (or another agent) left the main tree dirty with UNRELATED work
    (repo / "user-draft.txt").write_text("mine\n", encoding="utf-8")
    (repo / "hello.txt").write_text("v1 edited\n", encoding="utf-8")
    result = await worktrees.merge_back(repo, info["branch"])
    assert result["merged"] is True
    assert "unrelated" in result.get("note", "")
    # the WIP survives untouched next to the merged file
    assert (repo / "user-draft.txt").read_text(encoding="utf-8") == "mine\n"
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "v1 edited\n"
    assert (repo / "f.txt").read_text(encoding="utf-8") == "x\n"


async def test_merge_back_refuses_dirty_overlap(repo: Path):
    """Uncommitted main-tree files the merge must update still refuse —
    git would clobber them, and YAAH never stashes user work."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="1")
    info = worktrees.binding_for("1")
    (Path(wt) / "hello.txt").write_text("branch edit\n", encoding="utf-8")
    _git(Path(wt), "add", "-A")
    _git(Path(wt), "commit", "-q", "-m", "branch edit")
    # the user's uncommitted edit is to the SAME file the branch changes
    (repo / "hello.txt").write_text("my unfinished edit\n", encoding="utf-8")
    result = await worktrees.merge_back(repo, info["branch"])
    assert result["merged"] is False
    assert "hello.txt" in result["reason"]
    assert result["dirty_overlap"] == ["hello.txt"]
    # the WIP is intact and nothing from the branch landed
    assert (repo / "hello.txt").read_text(encoding="utf-8") == (
        "my unfinished edit\n"
    )


async def test_merge_back_conflict_aborts_clean(repo: Path):
    # a commit on main that will conflict with the worktree branch
    wt = await worktrees.ensure_isolated(str(repo), chat_id="1")
    info = worktrees.binding_for("1")
    (repo / "hello.txt").write_text("main edit\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main edit")
    (Path(wt) / "hello.txt").write_text("branch edit\n", encoding="utf-8")
    _git(Path(wt), "add", "-A")
    _git(Path(wt), "commit", "-q", "-m", "branch edit")
    result = await worktrees.merge_back(repo, info["branch"])
    assert result["merged"] is False and result.get("conflict") is True
    # the shared tree is left clean (merge aborted), on the original commit
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    status = _git(repo, "status", "--porcelain")
    assert status == ""


async def test_self_merge_lifecycle(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="7")
    (Path(wt) / "done.txt").write_text("1\n", encoding="utf-8")
    _git(Path(wt), "add", "-A")
    _git(Path(wt), "commit", "-q", "-m", "done")
    result = await worktrees.self_merge("7")
    assert result["merged"] is True
    assert (repo / "done.txt").exists()
    assert not Path(wt).exists()
    assert worktrees.binding_for("7") is None
    # unbound self-merge is a no-op, never an error
    noop = await worktrees.self_merge("7")
    assert noop.get("noop") is True


async def test_self_merge_refuses_and_salvages_dirty_worktree(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="8")
    (Path(wt) / "uncommitted.txt").write_text("half-done\n", encoding="utf-8")
    result = await worktrees.self_merge("8")
    assert result["merged"] is False
    assert "uncommitted" in result["reason"]
    assert not (repo / "uncommitted.txt").exists()
    # salvage patch exists next to where the worktree was
    salvages = list((repo / ".yaah" / "worktrees").glob("*.salvage.patch"))
    assert salvages, "dirty worktree must be salvaged before deletion"
    assert "half-done" in salvages[0].read_text(encoding="utf-8")
    assert worktrees.binding_for("8") is None


# ------------------------------------------------------- sub-agent finalize


async def test_finalize_pins_branch_on_first_line(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="1")
    info = worktrees.binding_for("1")
    (Path(wt) / "sub.txt").write_text("x\n", encoding="utf-8")
    _git(Path(wt), "add", "-A")
    _git(Path(wt), "commit", "-q", "-m", "sub")
    result = await worktrees.finalize_sub_agent(
        wt, {"status": "completed", "output": "all done", "turns": 2}
    )
    assert result["output"].startswith("[changes are on branch")
    assert info["branch"] in result["output"].splitlines()[0]
    assert result["worktree_branch"] == info["branch"]
    assert not Path(wt).exists()  # dir removed, branch kept for the parent
    assert info["branch"] in _git(repo, "branch", "--list", info["branch"])


async def test_finalize_notes_zero_commit_runs(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="1")
    result = await worktrees.finalize_sub_agent(
        wt, {"status": "completed", "output": "read-only work", "turns": 1}
    )
    assert "carries no commits" in result["output"].splitlines()[0]
    assert not Path(wt).exists()


# ----------------------------------------------------------------- mutex


async def test_merge_mutex_serializes_concurrent_sections(repo: Path):
    order = []

    async def worker(name: str):
        async with worktrees.merge_mutex(repo):
            order.append(f"{name}-in")
            await asyncio.sleep(0.05)
            order.append(f"{name}-out")

    await asyncio.gather(worker("a"), worker("b"))
    # sections never interleave
    assert order.index("a-in") < order.index("a-out")
    assert order.index("b-in") < order.index("b-out")


async def test_merge_mutex_stale_lockfile_taken_over(repo: Path):
    lf = worktrees._lockfile(repo)
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text("pid=0 t=0\n", encoding="utf-8")
    old = time.time() - (worktrees.STALE_LOCK_SECONDS + 10)
    os.utime(lf, (old, old))
    async with worktrees.merge_mutex(repo):
        assert not lf.exists() or lf.stat().st_mtime > old


# ---------------------------------------------------------------- reaper


async def test_reaper_salvages_then_removes_stale_worktrees(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="1")
    info = worktrees.binding_for("1")
    (Path(wt) / "orphan.txt").write_text("lost\n", encoding="utf-8")
    # orphan the session (simulate a crashed run) and age it past the TTL
    worktrees._chat_bindings.pop("1")
    worktrees._active[str(wt)] = {
        **info,
        "created": time.time() - worktrees._ttl() - 1,
    }
    result = await worktrees.reap_stale()
    assert str(wt) in result["reaped"]
    assert result["salvaged"], "dirty orphan must be salvaged before deletion"
    assert any(
        "lost" in Path(p).read_text(encoding="utf-8") for p in result["salvaged"]
    )
    assert not Path(wt).exists()
    # the branch stays (kept a few days for inspection)
    assert info["branch"] in _git(repo, "branch", "--list", info["branch"])


async def test_reaper_spares_live_sessions(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="1")
    info = worktrees.binding_for("1")
    worktrees._active[str(wt)] = {
        **info,
        "created": time.time() - worktrees._ttl() - 1,
    }
    result = await worktrees.reap_stale()
    assert str(wt) not in result["reaped"]
    assert Path(wt).exists()
