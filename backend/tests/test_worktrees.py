"""Issue #58: worktree-per-writing-agent + mutex-serialized merge-back.

Covers the harness contract end to end on real temp git repos: rebinding
(ensure_isolated), merge-back refusal rules (dirty worktree, zero commits,
dirty overlap with the merge, conflict abort), self_merge lifecycle,
sub-agent result finalization (branch on the first line), the reaper's
salvage-before-delete, and the UI hygiene filters.

Issue #98 / adr/0002: trash-aware merge-back — write provenance (model
vs tool), machine-shape fingerprints, drop-trash-then-merge, the probe/
retry protocol (final=False), and the background main-tree fast-forward.
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
    (Path(wt) / "draft.md").write_text("half-done\n", encoding="utf-8")
    result = await worktrees.self_merge("8")
    assert result["merged"] is False
    assert "draft.md" in result["reason"]
    assert not (repo / "draft.md").exists()
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


async def test_self_merge_drops_trash_and_merges(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="11")
    (Path(wt) / "feature.txt").write_text("work\n", encoding="utf-8")
    _commit_all(wt, "feature")
    # the exact incident shape: a redirect capture left uncommitted
    (Path(wt) / "tsc-out2.txt").write_text(
        "error TS2304: node_modules missing\n", encoding="utf-8"
    )
    result = await worktrees.self_merge("11")
    assert result["merged"] is True, result
    assert result.get("dropped_trash") == ["tsc-out2.txt"]
    assert (repo / "feature.txt").exists()
    assert not (repo / "tsc-out2.txt").exists()  # dropped, never merged
    assert not Path(wt).exists()
    assert worktrees.binding_for("11") is None


async def test_self_merge_still_refuses_authored_dirt(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="12")
    (Path(wt) / "feature.txt").write_text("work\n", encoding="utf-8")
    _commit_all(wt, "feature")
    (Path(wt) / "draft.md").write_text("# half-written\n", encoding="utf-8")
    result = await worktrees.self_merge("12")
    assert result["merged"] is False
    assert "draft.md" in result["reason"]
    assert not (repo / "draft.md").exists()
    salvages = list((repo / ".yaah" / "worktrees").glob("*.salvage.patch"))
    assert salvages and "half-written" in salvages[0].read_text(encoding="utf-8")


async def test_probe_retries_then_final_merge(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="13")
    (Path(wt) / "feature.txt").write_text("work\n", encoding="utf-8")
    _commit_all(wt, "feature")
    (Path(wt) / "draft.md").write_text("# half-authored\n", encoding="utf-8")
    # probe 1: authored dirt survives the drop pass -> retry_dirty
    probe = await worktrees.self_merge("13", final=False)
    assert probe.get("retry_dirty") is True
    assert any("draft.md" in p for p in probe.get("dirty", []))
    assert Path(wt).exists(), "probe must keep the worktree for the retry"
    assert worktrees.binding_for("13") is not None
    # the model cleans up: remove the authored leftover, then commit the
    # real work (the notes file)
    (Path(wt) / "draft.md").unlink()
    (Path(wt) / "notes.md").write_text("# kept\n", encoding="utf-8")
    _commit_all(wt, "notes")
    # probe 2: clean now -> ready, still no merge, no teardown
    probe2 = await worktrees.self_merge("13", final=False)
    assert probe2.get("clean") is True
    assert Path(wt).exists()
    # final: the real merge lands
    final = await worktrees.self_merge("13")
    assert final["merged"] is True, final
    assert (repo / "feature.txt").exists()
    assert (repo / "notes.md").exists()
    assert not Path(wt).exists()


async def test_probe_drops_trash_so_turn_ends_clean(repo: Path):
    wt = await worktrees.ensure_isolated(str(repo), chat_id="14")
    (Path(wt) / "feature.txt").write_text("work\n", encoding="utf-8")
    _commit_all(wt, "feature")
    (Path(wt) / "vitest-out.txt").write_text(
        "ALL TESTS FAILED\n" * 3, encoding="utf-8"
    )
    probe = await worktrees.self_merge("14", final=False)
    # the capture was droppable: probe reports clean, merges nothing
    assert probe.get("clean") is True
    assert probe.get("dropped_trash") == ["vitest-out.txt"]
    assert not (Path(wt) / "vitest-out.txt").exists()
    final = await worktrees.self_merge("14")
    assert final["merged"] is True
    assert (repo / "feature.txt").exists()


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

