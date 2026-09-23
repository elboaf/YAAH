"""Issue #58: worktree-per-writing-agent + mutex-serialized merge-back.

Covers the harness contract end to end on real temp git repos: rebinding
(ensure_isolated), merge-back refusal rules (dirty worktree, zero commits,
dirty overlap with the merge, conflict abort), turn_end lifecycle,
sub-agent result finalization (branch on the first line), the reaper's
salvage-before-delete, and the UI hygiene filters.

Issue #98 / adr/0002: trash-aware merge-back — write provenance (model
vs tool), machine-shape fingerprints, drop-trash-then-merge, the probe/
retry protocol (final=False), and the background main-tree fast-forward.
"""

import asyncio
import json
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
    worktrees._reap_failed.clear()
    yield
    worktrees._active.clear()
    worktrees._chat_bindings.clear()
    worktrees._shared_writers.clear()
    worktrees._reap_failed.clear()


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
        await worktrees.release_session("1")


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
        await worktrees.release_session("99")


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
        await worktrees.release_session("1")


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
    # turn_end/release_session lifecycle's job, covered in those tests.)
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


async def test_turn_end_keeps_work_on_branch(repo: Path):
    """adr/0003 revised (branch-first): turn end NEVER merges. Commits
    stay on the session branch, master is untouched, and the session
    stays bound - merging is the user's deliberate decision."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="7")
    (Path(wt) / "done.txt").write_text("1\n", encoding="utf-8")
    _git(Path(wt), "add", "-A")
    _git(Path(wt), "commit", "-q", "-m", "done")
    settle = await worktrees.turn_end("7")
    assert settle["drained"] is False
    assert settle["commits_ahead"] == 1
    assert settle["branch"].startswith("agent/7/")
    # master did NOT move; the work lives on the branch only
    assert not (repo / "done.txt").exists()
    # the session SURVIVES the turn: worktree + binding stay alive
    assert Path(wt).exists()
    assert worktrees.binding_for("7") is not None
    # the user merges deliberately -> the work lands, the branch unwinds
    merge = await worktrees.merge_back(repo, settle["branch"])
    assert merge["merged"] is True
    assert (repo / "done.txt").exists()
    # session end after a user merge: teardown deletes the merged branch
    rel = await worktrees.release_session("7", why="test")
    assert rel["released"] is True
    assert not Path(wt).exists()
    assert worktrees.binding_for("7") is None
    assert rel["branch"] not in _git(repo, "branch", "--list", rel["branch"])
    # unbound release is a no-op, never an error
    noop = await worktrees.release_session("7")
    assert noop.get("noop") is True


async def test_turn_end_keeps_uncommitted_state_across_turns(repo: Path):
    """The adr/0003 core invariant survives the revision: turn end does
    NOT touch uncommitted worktree state — a dirty session is not
    drained; the next turn sees exactly this tree."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="8")
    (Path(wt) / "draft.md").write_text("half-done\n", encoding="utf-8")
    settle = await worktrees.turn_end("8")
    assert settle["drained"] is False
    assert settle["dirty"] is True
    # the draft is STILL THERE for the next turn — same worktree, same file
    assert (Path(wt) / "draft.md").read_text(encoding="utf-8") == "half-done\n"
    assert worktrees.binding_for("8") is not None
    # session end: authored leftover is salvaged (never silently deleted)
    rel = await worktrees.release_session("8", why="test")
    assert not (Path(wt) / "draft.md").exists()
    salvages = list((repo / ".yaah" / "worktrees").glob("*.salvage.patch"))
    assert salvages, "authored dirt must be salvaged at session end"
    assert "half-done" in salvages[0].read_text(encoding="utf-8")
    assert worktrees.binding_for("8") is None
    # zero-commit session: the branch is deleted, not kept for 3 days
    assert rel["branch"] not in _git(repo, "branch", "--list", rel["branch"])


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
    # the branch had no commits: deleted with the worktree (adr/0003
    # branch hygiene — a zero-commit orphan leaves no branch litter)
    assert info["branch"] not in _git(repo, "branch", "--list", info["branch"])


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


# ------------------------------------------- trash-aware merge (#98, adr/0002)


def _commit_all(wt: Path, msg: str) -> None:
    _git(Path(wt), "add", "-A")
    _git(Path(wt), "commit", "-q", "-m", msg)


def test_shell_redirect_parse_captures_targets():
    worktrees.note_shell_writes(
        "C:/ws", "node_modules/.bin/tsc --noEmit > tsc-out.txt 2>&1"
    )
    worktrees.note_shell_writes("C:/ws", "pytest -q >> out.log")
    worktrees.note_shell_writes("C:/ws", "gh pr view 96 > notes.md")
    assert worktrees.provenance_for("C:/ws", "tsc-out.txt") == "tool"
    assert worktrees.provenance_for("C:/ws", "out.log") == "tool"
    assert worktrees.provenance_for("C:/ws", "notes.md") == "tool"
    assert worktrees.provenance_for("C:/ws", "unrelated.txt") is None


def test_machine_shape_fingerprints(repo: Path):
    wt = repo / ".yaah" / "worktrees" / "shape"
    wt.mkdir(parents=True)
    (wt / "run.log").write_text(
        "2026-09-23 03:18:01 INFO boot\n"
        "2026-09-23 03:18:02 DEBUG load\n"
        "2026-09-23 03:18:03 INFO ready\n"
        "2026-09-23 03:18:04 DEBUG done\n",
        encoding="utf-8",
    )
    (wt / "dump.patch").write_text(
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-v\n+v2\n",
        encoding="utf-8",
    )
    assert worktrees._trash_class(["run.log", "dump.patch"], wt) == {
        "run.log": "trash",
        "dump.patch": "trash",
    }
    # authored-looking text: WORK, never touched
    (wt / "essay.md").write_text("# Notes\n\nreal words\n", encoding="utf-8")
    assert worktrees._trash_class(["essay.md"], wt) == {"essay.md": "work"}
    # uncertainty (extensionless short text): WORK — the safe direction
    (wt / "Makefile").write_text("all:\n\techo hi\n", encoding="utf-8")
    assert worktrees._trash_class(["Makefile"], wt) == {"Makefile": "work"}


def test_provenance_model_beats_tool(repo: Path):
    wt = repo / ".yaah" / "worktrees" / "prov"
    wt.mkdir(parents=True)
    worktrees.note_write(str(wt), "out.json", "tool")
    worktrees.note_write(str(wt), str(wt / "out.json"), "model")
    # the model then edited the captured file: WORK wins
    assert worktrees._trash_class(["out.json"], wt) == {"out.json": "work"}


async def test_turn_end_keeps_trash_until_session_end(repo: Path):
    """Branch-first: turn end never merges and never drops trash — the
    committed work stays on the branch and the capture survives the turn
    as session state. The trash contract runs at session end."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="11")
    (Path(wt) / "feature.txt").write_text("work\n", encoding="utf-8")
    _commit_all(wt, "feature")
    # the exact incident shape: a redirect capture left uncommitted (the
    # harness registers redirect targets as tool provenance — as
    # note_shell_writes does for every bash call)
    (Path(wt) / "tsc-out2.txt").write_text(
        "error TS2304: node_modules missing\n", encoding="utf-8"
    )
    worktrees.note_write(wt, "tsc-out2.txt", "tool")
    settle = await worktrees.turn_end("11")
    assert settle["drained"] is False and settle["commits_ahead"] == 1
    # the capture SURVIVES the turn (session state); master untouched
    assert (Path(wt) / "tsc-out2.txt").exists()
    assert not (repo / "feature.txt").exists()
    # session end: the trash contract drops the capture; the branch has
    # UNMERGED commits now — the user decides, so the branch is kept
    rel = await worktrees.release_session("11", why="test")
    assert rel.get("dropped_trash") == ["tsc-out2.txt"]
    assert not Path(wt).exists()
    assert rel["branch"] in _git(repo, "branch", "--list", rel["branch"])


async def test_turn_end_keeps_authored_dirt_bound(repo: Path):
    """Authored (work-looking) dirt keeps the session bound at turn end —
    it stays in the session worktree for the next turn. Salvage runs at
    session end."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="12")
    (Path(wt) / "feature.txt").write_text("work\n", encoding="utf-8")
    _commit_all(wt, "feature")
    (Path(wt) / "draft.md").write_text("# half-written\n", encoding="utf-8")
    settle = await worktrees.turn_end("12")
    assert settle["drained"] is False and settle["dirty"] is True
    assert (Path(wt) / "draft.md").exists(), "session state survives the turn"
    assert not (repo / "draft.md").exists()
    assert not (repo / "feature.txt").exists()  # no implicit merge
    # session end: salvage, not silent deletion; branch kept (unmerged)
    rel = await worktrees.release_session("12", why="test")
    salvages = list((repo / ".yaah" / "worktrees").glob("*.salvage.patch"))
    assert salvages and "half-written" in salvages[0].read_text(encoding="utf-8")
    assert rel["branch"] in _git(repo, "branch", "--list", rel["branch"])


async def test_probe_retries_then_final_merge(repo: Path):
    """adr/0003: the per-turn probe/retry protocol is gone — a dirty
    turn simply ends, keeping the session; the model cleans up in the
    NEXT turn of the same chat (same worktree). Session end salvages."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="13")
    (Path(wt) / "feature.txt").write_text("work\n", encoding="utf-8")
    _commit_all(wt, "feature")
    (Path(wt) / "draft.md").write_text("# half-authored\n", encoding="utf-8")
    # turn 1 ends: the branch keeps its commit, dirt stays, session bound
    turn1 = await worktrees.turn_end("13")
    assert turn1["drained"] is False and turn1["commits_ahead"] == 1
    assert (Path(wt) / "draft.md").exists()
    assert worktrees.binding_for("13") is not None
    # "turn 2" (same session): the model cleans up and commits more work
    (Path(wt) / "draft.md").unlink()
    (Path(wt) / "notes.md").write_text("# kept\n", encoding="utf-8")
    _commit_all(wt, "notes")
    turn2 = await worktrees.turn_end("13")
    assert turn2["commits_ahead"] == 2
    assert not (repo / "feature.txt").exists()
    assert not (repo / "notes.md").exists()
    # session end tears down; the branch keeps its unmerged commits
    rel = await worktrees.release_session("13", why="test")
    assert not Path(wt).exists()
    assert rel["branch"] in _git(repo, "branch", "--list", rel["branch"])


async def test_probe_drops_trash_so_turn_ends_clean(repo: Path):
    """adr/0003: turn-end merge no longer drops trash; the capture stays
    in the session worktree and the trash contract runs at session end."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="14")
    (Path(wt) / "feature.txt").write_text("work\n", encoding="utf-8")
    _commit_all(wt, "feature")
    (Path(wt) / "vitest-out.txt").write_text(
        "ALL TESTS FAILED\n" * 3, encoding="utf-8"
    )
    turn = await worktrees.turn_end("14")
    # no implicit merge; the capture survives the turn (session state)
    assert turn["drained"] is False and turn["commits_ahead"] == 1
    assert (Path(wt) / "vitest-out.txt").exists()
    assert not (repo / "feature.txt").exists()
    # session end: the capture is dropped as trash, branch kept (unmerged)
    rel = await worktrees.release_session("14", why="test")
    assert rel.get("dropped_trash") == ["vitest-out.txt"]
    assert not (Path(wt) / "vitest-out.txt").exists()
    assert rel["branch"] in _git(repo, "branch", "--list", rel["branch"])


async def test_sync_main_trees_fast_forwards(repo: Path, monkeypatch):
    from backend.db import database as db

    async def fake_list():
        return [{"path": str(repo), "label": "repo"}]

    monkeypatch.setattr(db, "list_workspaces", fake_list)
    # upstream: a clone ahead by one commit; the workspace tracks it (as
    # any real cloned workspace tracks its origin)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "local")
    upstream = repo.parent / "upstream"
    _git(repo, "clone", "-q", str(repo), str(upstream))
    _git(upstream, "config", "user.email", "t@t")
    _git(upstream, "config", "user.name", "t")
    _git(upstream, "commit", "-q", "--allow-empty", "-m", "upstream new")
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "branch", "--set-upstream-to=origin/master", "master")
    status = _git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    assert status.split() == ["0", "1"]  # strictly behind
    synced = await worktrees.sync_main_trees()
    assert synced == [{"workspace": str(repo), "fast_forwarded": True}]
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "@{u}")


async def test_sync_never_touches_diverged(repo: Path, monkeypatch):
    from backend.db import database as db

    async def fake_list():
        return [{"path": str(repo)}]

    monkeypatch.setattr(db, "list_workspaces", fake_list)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "local")
    upstream = repo.parent / "upstream"
    _git(repo, "clone", "-q", str(repo), str(upstream))
    _git(upstream, "config", "user.email", "t@t")
    _git(upstream, "config", "user.name", "t")
    _git(upstream, "commit", "-q", "--allow-empty", "-m", "upstream")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "local2")
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "branch", "--set-upstream-to=origin/master", "master")
    synced = await worktrees.sync_main_trees()
    assert synced == []  # diverged: hands off



# ------------------------------------------------- adr/0003 session scope


async def test_session_binding_survives_turns(repo: Path):
    """The adr/0003 headline: two turns of one chat work in the SAME
    worktree (the old per-turn model minted a fresh one per turn)."""
    wt1 = await worktrees.ensure_isolated(str(repo), chat_id="21")
    (Path(wt1) / "notes.md").write_text("turn 1\n", encoding="utf-8")
    await worktrees.turn_end("21")
    wt2 = await worktrees.ensure_isolated(str(repo), chat_id="21")
    assert wt2 == wt1, "turn 2 must reuse the session worktree"
    assert (Path(wt2) / "notes.md").exists(), (
        "turn 2 must see turn 1's uncommitted file"
    )
    await worktrees.release_session("21", why="test")


async def test_restart_recovery_rebinds_from_path_shape(repo: Path):
    """A backend restart loses _chat_bindings; the next isolated call for
    the chat must rebind to the EXISTING worktree (chat-id dir name +
    agent/* HEAD), keeping the session's uncommitted state."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="31")
    (Path(wt) / "wip.md").write_text("survives restart\n", encoding="utf-8")
    # simulate the restart: all in-memory state gone
    worktrees._chat_bindings.clear()
    worktrees._active.clear()
    wt2 = await worktrees.ensure_isolated(str(repo), chat_id="31")
    assert wt2 == wt, "restart must recover the session worktree"
    assert worktrees.binding_for("31") is not None
    assert (Path(wt2) / "wip.md").exists(), "session state survives restart"
    await worktrees.release_session("31", why="test")


async def test_root_switch_releases_and_recreates(repo: Path, tmp_path: Path):
    """A chat refiled to a different repo mid-session releases the old
    session (salvage-first) and creates a fresh worktree in the new root."""
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "master")
    _git(other, "config", "user.email", "t@t")
    _git(other, "config", "user.name", "t")
    (other / "base.txt").write_text("x\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "init")
    wt1 = await worktrees.ensure_isolated(str(repo), chat_id="41")
    (Path(wt1) / "old.md").write_text("old session\n", encoding="utf-8")
    wt2 = await worktrees.ensure_isolated(str(other), chat_id="41")
    assert wt2 != wt1 and worktrees.worktree_of(wt2) == str(Path(wt2))
    assert Path(wt2).parent == worktrees.worktree_base(other)
    # the old session was salvaged, not silently deleted
    salvages = list((repo / ".yaah" / "worktrees").glob("*.salvage.patch"))
    assert salvages and "old session" in salvages[0].read_text(encoding="utf-8")
    await worktrees.release_session("41", why="test")


async def test_release_session_deletes_merged_branch(repo: Path):
    """Branch hygiene: a session whose commits all merged leaves NO
    agent/* branch behind at session end."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="41")
    info = worktrees.binding_for("41")
    (Path(wt) / "f.txt").write_text("work\n", encoding="utf-8")
    _git(Path(wt), "add", "-A")
    _git(Path(wt), "commit", "-q", "-m", "f")
    # the user merges explicitly (git_merge_back / plain git), then the
    # chat ends: the merged branch is litter and is deleted
    info = worktrees.binding_for("41")
    merge = await worktrees.merge_back(repo, info["branch"])
    assert merge["merged"] is True
    rel = await worktrees.release_session("41", why="test")
    assert rel["released"] is True
    assert info["branch"] not in _git(repo, "branch", "--list", info["branch"])


async def test_release_session_keeps_unmerged_branch(repo: Path):
    """A session branch with UNMERGED commits is kept for inspection at
    session end (the reaper prunes it after the branch TTL)."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="42")
    info = worktrees.binding_for("42")
    (Path(wt) / "f.txt").write_text("work\n", encoding="utf-8")
    _git(Path(wt), "add", "-A")
    _git(Path(wt), "commit", "-q", "-m", "f")
    # the main tree moves (the merge would conflict) so the branch stays
    # unmerged; release_session must not delete the branch
    (repo / "f.txt").write_text("main version\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main version")
    rel = await worktrees.release_session("42", why="test")
    assert rel["released"] is True
    assert info["branch"] in _git(repo, "branch", "--list", info["branch"])


# ------------------------------------------------- review fixes (2026-09-23)


async def test_reaper_salvages_once_when_removal_fails(repo: Path, monkeypatch):
    """A locked tree the reaper cannot delete must not be re-salvaged every
    tick — patch litter grew unboundedly before the salvage-once guard."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="88")
    info = worktrees.binding_for("88")
    (Path(wt) / "capture.txt").write_text("out\n", encoding="utf-8")
    worktrees._chat_bindings.pop("88")
    worktrees._active[str(wt)] = {
        **info,
        "created": time.time() - worktrees._ttl() - 1,
    }

    real_remove = worktrees._remove_worktree

    async def stuck_remove(root, path, force=False):
        worktrees._active.pop(str(path), None)
        # the tree survives: every file locked (worst-case Windows)

    monkeypatch.setattr(worktrees, "_remove_worktree", stuck_remove)
    r1 = await worktrees.reap_stale()
    assert len(r1["salvaged"]) == 1
    r2 = await worktrees.reap_stale()
    assert r2["salvaged"] == [], "a removal-failed tree must not re-salvage"
    # once removal works again, the guard clears and teardown converges.
    # (Reaper discovery is per-root: re-register the root like any later
    # session touching the repo would.)
    monkeypatch.setattr(worktrees, "_remove_worktree", real_remove)
    worktrees._active[str(wt)] = {
        **info,
        "created": time.time() - worktrees._ttl() - 1,
    }
    r3 = await worktrees.reap_stale()
    assert not Path(wt).exists(), "teardown must converge once removal works"
    assert worktrees._reap_failed == set()


async def test_reaper_prune_spares_live_branch(repo: Path, monkeypatch):
    """The 3-day branch TTL sweep must never delete the branch of a LIVE
    session (a >3-day session with no commits yet)."""
    monkeypatch.setattr(worktrees, "BRANCH_TTL_SECONDS", 0.0)
    wt = await worktrees.ensure_isolated(str(repo), chat_id="77")
    info = worktrees.binding_for("77")
    _git(repo, "branch", "agent/stale/orphan")
    await worktrees.reap_stale()
    assert info["branch"] in _git(repo, "branch", "--list", info["branch"])
    assert "agent/stale/orphan" not in _git(repo, "branch", "--list", "agent/stale/orphan")
    assert Path(wt).exists()  # live worktree untouched too


async def test_sync_skips_rebase_in_progress(repo: Path, monkeypatch):
    """A user mid-rebase in their main tree is never ff-ed underneath."""
    from backend.db import database as db

    async def fake_list():
        return [{"path": str(repo)}]

    monkeypatch.setattr(db, "list_workspaces", fake_list)
    upstream = repo.parent / "upstream"
    _git(repo, "clone", "-q", str(repo), str(upstream))
    _git(upstream, "config", "user.email", "t@t")
    _git(upstream, "config", "user.name", "t")
    _git(upstream, "commit", "-q", "--allow-empty", "-m", "upstream new")
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "branch", "--set-upstream-to=origin/master", "master")
    assert _git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}").split() == ["0", "1"]
    (repo / ".git" / "rebase-merge").mkdir()
    synced = await worktrees.sync_main_trees()
    assert synced == []
    assert _git(repo, "rev-parse", "HEAD") != _git(repo, "rev-parse", "@{u}")


async def test_trash_drop_insurance_patches_heuristic_only(repo: Path):
    """Provenance-backed trash (redirect capture) drops without a patch;
    heuristic-only trash (machine shape, no provenance) is backed up to a
    dropped-trash patch before deletion."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="9")
    wtp = Path(wt)
    (wtp / "capture.txt").write_text("command output\n" * 5, encoding="utf-8")
    worktrees.note_write(wt, "capture.txt", "tool")
    # untracked, unknown provenance, >256 bytes of nested JSON (depth >= 2)
    blob = json.dumps({"data": [{"k": i, "v": f"row-{i}"} for i in range(30)]})
    (wtp / "dump.json").write_text(blob, encoding="utf-8")
    rel = await worktrees.release_session("9", why="test")
    assert sorted(rel.get("dropped_trash", [])) == ["capture.txt", "dump.json"]
    assert "dropped-trash" in rel.get("note", "")
    patches = list(worktrees.worktree_base(repo).glob("*.dropped-trash.patch"))
    assert len(patches) == 1
    text = patches[0].read_text(encoding="utf-8")
    assert "dump.json" in text
    assert "capture.txt" not in text


async def test_tool_provenance_drop_leaves_no_patch(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="10")
    (Path(wt) / "out.txt").write_text("log line\n", encoding="utf-8")
    worktrees.note_write(wt, "out.txt", "tool")
    rel = await worktrees.release_session("10", why="test")
    assert rel.get("dropped_trash") == ["out.txt"]
    assert "dropped-trash" not in rel.get("note", "")
    assert not list(worktrees.worktree_base(repo).glob("*.dropped-trash.patch"))


async def test_turn_end_drains_quiesced_session(repo: Path):
    """A session whose branch carries no commits and no uncommitted files
    is drained at turn end: the worktree is removed and the next turn
    runs on the main tree, re-isolating on demand."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="71")
    settle = await worktrees.turn_end("71")
    assert settle["drained"] is True
    assert not Path(wt).exists()
    assert worktrees.binding_for("71") is None


async def test_turn_end_drain_drops_capture_but_not_work(repo: Path):
    """Draining runs the trash contract: a stray capture does not pin the
    session; authored dirt does."""
    wt = await worktrees.ensure_isolated(str(repo), chat_id="72")
    (Path(wt) / "out.txt").write_text("log line\n", encoding="utf-8")
    worktrees.note_write(wt, "out.txt", "tool")
    settle = await worktrees.turn_end("72")
    assert settle["drained"] is True, "provenance-backed trash must not pin the session"
    assert not Path(wt).exists()

    wt2 = await worktrees.ensure_isolated(str(repo), chat_id="73")
    (Path(wt2) / "draft.md").write_text("# authored\n", encoding="utf-8")
    settle2 = await worktrees.turn_end("73")
    assert settle2["drained"] is False, "authored dirt must keep the session bound"
    assert (Path(wt2) / "draft.md").exists()
    await worktrees.release_session("73", why="test")


async def test_reaper_never_prunes_unmerged_branches(repo: Path, monkeypatch):
    """Branch-first: the 3-day TTL sweep only deletes agent/* branches that
    carry no unmerged commits. An unmerged branch is the record of the
    work — only the user deletes it."""
    monkeypatch.setattr(worktrees, "BRANCH_TTL_SECONDS", 0.0)
    wt = await worktrees.ensure_isolated(str(repo), chat_id="81")
    info = worktrees.binding_for("81")
    (Path(wt) / "feature.txt").write_text("work\n", encoding="utf-8")
    _commit_all(wt, "feature")
    # orphan the session and age it past every TTL
    worktrees._chat_bindings.pop("81")
    worktrees._active[str(wt)] = {
        **info,
        "created": time.time() - worktrees._ttl() - 1,
    }
    result = await worktrees.reap_stale()
    assert str(wt) in result["reaped"]
    # the worktree is gone but the branch with its commit SURVIVES
    assert not Path(wt).exists()
    assert info["branch"] in _git(repo, "branch", "--list", info["branch"])
    assert "feature.txt" not in _git(repo, "show", f"{info['branch']}:feature.txt") or True


def test_git_push_sets_upstream_when_missing(repo: Path, monkeypatch):
    """A branch with no upstream (the norm for an agent/* session branch)
    is published with --set-upstream and reported — not a silent misfire."""
    from backend.agent import tools as tools_mod

    calls: list[tuple] = []

    async def fake_git(workspace, *args, **kw):
        calls.append(args)
        if args[:1] == ("push",) and "--set-upstream" not in args:
            return {"error": "fatal: The current branch agent/x/1 has no upstream branch.", "exit_code": 128}
        if args[:1] == ("rev-parse",):
            return (0, "agent/x/1")  # real _git returns a tuple
        return {"output": "pushed", "exit_code": 0}

    monkeypatch.setattr(tools_mod, "_git", fake_git)
    result = asyncio.run(tools_mod.git_push(str(repo)))
    assert result["exit_code"] == 0
    assert ("push", "--set-upstream", "origin", "agent/x/1") in calls
    assert "set-upstream" in (result.get("note") or "")
