"""#354: the landing module core - ``land()`` executes the ADR-0014
safe-sync contract as verified code (ADR-0016).

Behavioral tests: REAL landings in temp-repo fixtures (the #278
``_repo_with_commit`` precedent; the #357 suite's monkeypatch and
cancel-shape idioms). Covered:

- happy path: two-parent landing commit, ref move, reset, post-verify
- fast-forward and already-contained classifications (tree comparison)
- dirty primary: scoped sweep carries tracked + staged ONLY (untracked
  noise stays untracked and survives the reset), restore command named
- conflicted merge resolves RUN-wins with the file list (ADR-0015 §3)
- moved-target-under-lock triggers a re-merge; the lineage is extended,
  never replaced (also proven via a CAS-refused update-ref)
- mid-merge / rebase / cherry-pick primaries refuse as manual-only
- the skip-the-sync-leg class is UN-WRITABLE: a failed reset leg fails
  the report loudly (post-verify), never a silent ok
- cancellation: before the point-of-no-return the landing aborts with
  the ref unmoved; inside it the region completes (never half-applied)
- unmerged index entries refuse manual-only at the freeze probe (the
  half-finished-conflict class); the sweep's wide fallback is unit-pinned
- staged renames sweep both rename sides
"""
import asyncio
import json
import subprocess

import pytest

from backend.agent import gitexec, landing


def _git(cwd, *args, check=True, input=None):
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=not isinstance(input, bytes),
        timeout=30,
        check=False,
        input=input,
    )
    if check:
        assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    out = proc.stdout.strip()
    return out.decode("utf-8") if isinstance(out, bytes) else out


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


def _commit_all(repo, msg):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)


def _write(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")


def _advance_master(repo, name="master.txt"):
    """A real commit on master (the fixture tree is clean after
    _setup_run_branch - the run change lives on the run branch)."""
    _write(repo, name, "master change\n")
    _commit_all(repo, "master advances")


def _setup_run_branch(repo, hello="run version\n"):
    """A run branch with one committed change, master left checked out
    at its own (unmoved) tip."""
    _git(repo, "checkout", "-qb", "run/test-77")
    _write(repo, "hello.txt", hello)
    _commit_all(repo, "run change")
    run_tip = _git(repo, "rev-parse", "run/test-77")
    _git(repo, "checkout", "-q", "master")
    return run_tip


def _land(repo, **kw):
    kwargs = {
        "workspace": str(repo),
        "run_branch": "run/test-77",
        "chat_id": 77,
        "title": "test",
    }
    kwargs.setdefault("lock", asyncio.Lock())
    kwargs.update(kw)
    return landing.land("master", **kwargs)


def _steps(report):
    return [s["step"] for s in report["steps"]]


def _step(report, name):
    for s in report["steps"]:
        if s["step"] == name:
            return s
    raise AssertionError(f"no step named {name}: {_steps(report)}")


def _master_tip(repo):
    return _git(repo, "rev-parse", "master")


def _is_ancestor(repo, sha, ref="master"):
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, ref],
        cwd=repo, capture_output=True, text=True, timeout=30, check=False,
    )
    return proc.returncode == 0


def _fake_unmerged(repo, path):
    """A REAL unmerged index entry: stage 0 removed, stages 1/2/3 in.
    BYTES input: text=True translates \n to CRLF on Windows and
    --index-info then silently ignores every path ("Ignoring path")."""
    b1 = _git(repo, "hash-object", "-w", "--stdin", input=b"ancestors\n")
    b2 = _git(repo, "hash-object", "-w", "--stdin", input=b"ours\n")
    b3 = _git(repo, "hash-object", "-w", "--stdin", input=b"theirs\n")
    _git(repo, "rm", "--cached", "-q", path)
    payload = (
        f"100644 {b1} 1\t{path}\n100644 {b2} 2\t{path}\n100644 {b3} 3\t{path}\n"
    ).encode("ascii")
    proc = subprocess.run(
        ["git", "update-index", "--index-info"],
        cwd=repo,
        input=payload,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert b"Ignoring path" not in proc.stderr, proc.stderr


# --- the executor's spine ---------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_two_parent_merge_lands_and_post_verifies(tmp_path):
    """AC: happy path lands and post-verifies. The default (module) lock
    path is exercised here - it is this file's only default-lock user,
    so no other test's event loop ever re-binds it."""
    repo = _repo_with_commit(tmp_path)
    run_tip = _setup_run_branch(repo)
    _advance_master(repo)  # force a real merge, not ff

    report = await _land(repo, lock=None)
    assert report["ok"], report
    tip = _master_tip(repo)
    assert report["landing_tip"] == tip
    parents = _git(repo, "log", "-1", "--format=%P").split()
    assert len(parents) == 2
    assert _is_ancestor(repo, run_tip)  # run branch carried into lineage
    assert _is_ancestor(repo, _git(repo, "rev-parse", "master^"))
    assert _git(repo, "rev-parse", "HEAD") == tip
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert _steps(report) == [
        "freeze-probe",
        "tips",
        "wip-sweep",
        "merge",
        "freshness",
        "update-ref",
        "reset",
        "post-verify",
    ]
    assert json.dumps(report)  # serializable, the wt_sweep convention


@pytest.mark.asyncio
async def test_fast_forward_when_target_is_behind(tmp_path):
    """The target never moved since the branch point: the run tip
    fast-forwards it - no synthetic merge commit is invented."""
    repo = _repo_with_commit(tmp_path)
    run_tip = _setup_run_branch(repo)  # master still at the branch point

    report = await _land(repo)
    assert report["ok"], report
    assert report["landing_tip"] == run_tip
    assert _master_tip(repo) == run_tip
    assert "fast-forward" in _step(report, "merge")["detail"]


@pytest.mark.asyncio
async def test_already_contained_is_a_noop_landing(tmp_path):
    """The run branch is already an ancestor of the target: the landing
    tip IS the target tip (update-ref degrades to a no-op CAS), the
    tree never changes, and the report says so."""
    repo = _repo_with_commit(tmp_path)
    _git(repo, "branch", "run/test-77")  # run tip == current master tip
    _advance_master(repo, "adv.txt")

    pre = _master_tip(repo)
    report = await _land(repo)
    assert report["ok"], report
    assert report["landing_tip"] == pre
    assert _master_tip(repo) == pre
    assert "already contained" in _step(report, "merge")["detail"]
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""


@pytest.mark.asyncio
async def test_unknown_target_fails_at_the_tips_step(tmp_path):
    repo = _repo_with_commit(tmp_path)
    _setup_run_branch(repo)
    report = await landing.land(
        "nope",
        workspace=str(repo),
        run_branch="run/test-77",
        chat_id=77,
        title="test",
        lock=asyncio.Lock(),
    )
    assert not report["ok"]
    assert report["failed_step"] == "tips"
    assert report["landing_tip"] is None


# --- the scoped wip sweep ---------------------------------------------------


@pytest.mark.asyncio
async def test_dirty_primary_sweeps_tracked_and_staged_only(tmp_path):
    """AC: dirty primary -> wip branch carries tracked+staged changes
    ONLY; the untracked file stays in the tree, uncommitted; the
    landing completes and the primary ends clean."""
    repo = _repo_with_commit(tmp_path)
    _setup_run_branch(repo)
    _advance_master(repo)  # real merge alongside the dirt
    _write(repo, "hello.txt", "human wip\n")  # tracked modification
    _write(repo, "staged.txt", "staged\n")
    _git(repo, "add", "staged.txt")  # staged add
    _write(repo, "untracked.txt", "noise\n")  # untracked noise

    pre = _master_tip(repo)
    report = await _land(repo)
    assert report["ok"], report
    assert report["wip_branch"] == "wip/test-77"
    assert set(report["wip_files"]) == {"hello.txt", "staged.txt"}
    assert report["restore_command"] == "git cherry-pick wip/test-77"

    # the wip commit's parent is the PRE-landing tip (ADR-0015 §2)
    assert _git(repo, "log", "-1", "--format=%P", "wip/test-77") == pre
    # the wip tree carries the whole primary index, untracked excluded
    tree = _git(repo, "rev-parse", "wip/test-77^{tree}")
    names = {ln.split("\t")[1] for ln in _git(repo, "ls-tree", tree).splitlines()}
    assert "untracked.txt" not in names
    # the wip CHANGES are the commit's diff vs its parent: exactly the
    # human's tracked+staged state (the tree itself is a full snapshot
    # of the primary's index, master's committed files included)
    diff = {
        ln.strip()
        for ln in _git(repo, "diff", "--name-only", pre, "wip/test-77").splitlines()
    }
    assert diff == {"hello.txt", "staged.txt"}
    assert _git(repo, "cat-file", "-p", f"{tree}:hello.txt") == "human wip"
    # untracked noise: still on disk after the reset
    assert (repo / "untracked.txt").read_text(encoding="utf-8") == "noise\n"
    # the primary is clean (tracked) and landed
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert _is_ancestor(repo, pre)


@pytest.mark.asyncio
async def test_staged_rename_sweeps_both_sides(tmp_path):
    """A staged rename's folded record hides a real deletion: both the
    new path and the original path are swept, so the wip tree carries
    the rename."""
    repo = _repo_with_commit(tmp_path)
    _write(repo, "ren_src.txt", "ren\n")
    _commit_all(repo, "add ren_src")
    _setup_run_branch(repo)
    _git(repo, "mv", "ren_src.txt", "ren_dst.txt")  # staged rename

    report = await _land(repo)
    assert report["ok"], report
    assert set(report["wip_files"]) == {"ren_src.txt", "ren_dst.txt"}
    tree = _git(repo, "rev-parse", "wip/test-77^{tree}")
    names = {ln.split("\t")[1] for ln in _git(repo, "ls-tree", tree).splitlines()}
    assert "ren_dst.txt" in names and "ren_src.txt" not in names


@pytest.mark.asyncio
async def test_unmerged_index_refuses_manual_only(tmp_path):
    """A half-finished conflict resolution - unmerged index entries
    without merge markers - is the #353 manual-only class: staging it
    against the temp copy would 'resolve' only the copy, leaving the
    human's real index conflicted and post-verify unpassable. The
    freeze probe refuses before anything runs."""
    repo = _repo_with_commit(tmp_path)
    _setup_run_branch(repo)
    _write(repo, "c2.txt", "committed\n")
    _commit_all(repo, "add c2")
    _fake_unmerged(repo, "c2.txt")

    pre = _master_tip(repo)
    report = await _land(repo)
    assert not report["ok"]
    assert report["manual_only"] is True
    assert "unmerged" in report["reason"]
    assert _steps(report) == ["freeze-probe"]
    assert _master_tip(repo) == pre


@pytest.mark.asyncio
async def test_wide_fallback_sweep_carries_unmerged_paths(tmp_path):
    """Unit: given a REAL unmerged index, the sweep's wide fallback is a
    superset - add -A resolves the unmerged path against the working
    tree and the snapshot commits it. (The executor refuses this state
    at the freeze probe; this drives the internal seam to pin the
    sweep's own behavior.) The human's real index stays untouched."""
    repo = _repo_with_commit(tmp_path)
    _write(repo, "c2.txt", "committed\n")
    _commit_all(repo, "add c2")
    _fake_unmerged(repo, "c2.txt")
    # the worktree holds the human's (uncommitted) resolution
    _write(repo, "c2.txt", "worktree resolution\n")
    tip = _git(repo, "rev-parse", "HEAD")

    step, swept, _untracked = await landing._wip_sweep(
        str(repo), tip, "wip/unittest", 60.0
    )
    assert step["ok"], step
    assert "fallback" in step["detail"]
    assert "c2.txt" in swept
    tree = _git(repo, "rev-parse", "wip/unittest^{tree}")
    names = {ln.split("\t")[1] for ln in _git(repo, "ls-tree", tree).splitlines()}
    assert "c2.txt" in names
    # the real index still holds the conflict - the sweep never touched it
    st = _git(repo, "status", "--porcelain=v2")
    assert "UU" in st


@pytest.mark.asyncio
async def test_sweep_failure_refuses_without_touching_anything(tmp_path):
    """A sweep that cannot stage leaves the primary untouched: no wip
    branch, no ref move, no reset - the report says so."""
    repo = _repo_with_commit(tmp_path)
    _setup_run_branch(repo)
    _write(repo, "hello.txt", "human wip\n")
    pre = _master_tip(repo)

    orig = gitexec.run_git

    async def failing_stage(workspace, *args, timeout=None, env=None):
        if args[:1] == ("update-index",):
            return 1, "simulated: staging failed"
        return await orig(workspace, *args, timeout=timeout, env=env)

    gitexec.run_git = failing_stage
    try:
        report = await _land(repo)
    finally:
        gitexec.run_git = orig

    assert not report["ok"]
    assert report["failed_step"] == "wip-sweep"
    assert "untouched" in report["reason"]
    assert report["wip_branch"] is None
    assert _master_tip(repo) == pre


# --- the plumbing merge -----------------------------------------------------


@pytest.mark.asyncio
async def test_conflicted_merge_resolves_run_wins_with_file_list(tmp_path):
    """AC: conflicted merge resolves run-wins with the file list. The
    target is branch 1 (ours), the run branch branch 2 (theirs): the
    landed content of the conflicted file is the RUN side's."""
    repo = _repo_with_commit(tmp_path)
    _write(repo, "c.txt", "base line 1\nbase line 2\n")
    _commit_all(repo, "base c")
    _git(repo, "checkout", "-qb", "run/test-77")
    _write(repo, "c.txt", "run side line 1\nbase line 2\n")
    _commit_all(repo, "run change")
    _git(repo, "checkout", "-q", "master")
    _write(repo, "c.txt", "master side line 1\nbase line 2\n")
    _commit_all(repo, "master change")

    report = await _land(repo)
    assert report["ok"], report
    assert report["conflicts_resolved"] == ["c.txt"]
    assert _git(repo, "cat-file", "-p", "master:c.txt") == (
        "run side line 1\nbase line 2"
    )
    assert "run-wins" in _step(report, "merge")["detail"]


# --- freshness, CAS, lineage -------------------------------------------------


@pytest.mark.asyncio
async def test_moved_target_under_lock_remerges_and_extends_lineage(tmp_path):
    """AC: moved-target-under-lock triggers re-merge, never lineage
    replacement. A real out-of-band commit lands on master between the
    merge and the freshness re-check; the module re-merges from the new
    tip and the final lineage contains BOTH the out-of-band commit and
    the run branch."""
    repo = _repo_with_commit(tmp_path)
    run_tip = _setup_run_branch(repo)
    _advance_master(repo)

    calls = []
    orig = landing._merge_leg

    async def spy(root, pre_tip, tip_run, timeout):
        step, tip, conflicts = await orig(root, pre_tip, tip_run, timeout)
        calls.append((pre_tip, tip))
        if len(calls) == 1:
            # out-of-band human landing while the merge is computed
            _write(repo, "ooo.txt", "out of band\n")
            _commit_all(repo, "out-of-band human commit")
        return step, tip, conflicts

    landing._merge_leg = spy
    try:
        report = await _land(repo)
    finally:
        landing._merge_leg = orig

    assert report["ok"], report
    assert len(calls) == 2
    remerge = _step(report, "freshness-remerge")
    assert remerge["ok"] and "recomputed" in remerge["detail"]
    ooo = _git(repo, "rev-parse", "master^")  # parent of the final merge commit
    assert _is_ancestor(repo, ooo)  # the out-of-band commit survives
    assert _is_ancestor(repo, run_tip)


@pytest.mark.asyncio
async def test_cas_ref_move_refuses_to_clobber_a_moved_target(tmp_path):
    """The target moves between the freshness pass and the ref move: the
    CAS update-ref fails, the report fails loudly, and master keeps the
    out-of-band commit - a lineage is never replaced."""
    repo = _repo_with_commit(tmp_path)
    _setup_run_branch(repo)
    _advance_master(repo)

    orig = gitexec.run_git

    async def move_master_under_it(workspace, *args, timeout=None, env=None):
        if args[:1] == ("update-ref",) and len(args) == 4:
            _write(repo, "ooo.txt", "out of band\n")
            _commit_all(repo, "out-of-band human commit")
        return await orig(workspace, *args, timeout=timeout, env=env)

    gitexec.run_git = move_master_under_it
    try:
        report = await _land(repo)
    finally:
        gitexec.run_git = orig

    assert not report["ok"]
    assert report["failed_step"] == "update-ref"
    assert "update-ref failed" in report["reason"]
    # master kept the human commit: the ref move was refused
    ooo = _git(repo, "rev-parse", "master")
    assert _git(repo, "log", "-1", "--format=%s", ooo) == "out-of-band human commit"
    assert _is_ancestor(repo, "run/test-77", "master") is False


# --- freeze probe (manual-only) ----------------------------------------------


def _fake_mid_state(repo, marker):
    gd = repo / ".git"
    if marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD"):
        (gd / marker).write_text(_master_tip(repo) + "\n", encoding="utf-8")
    else:  # rebase-merge / rebase-apply directories
        (gd / marker).mkdir()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker", ["MERGE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge", "rebase-apply"]
)
async def test_in_progress_primary_refuses_manual_only(tmp_path, marker):
    """AC: mid-merge primary refuses manual-only - before anything runs:
    no sweep, no merge, no ref move, no reset."""
    repo = _repo_with_commit(tmp_path)
    _setup_run_branch(repo)
    _fake_mid_state(repo, marker)

    pre = _master_tip(repo)
    report = await _land(repo)
    assert not report["ok"]
    assert report["manual_only"] is True
    assert marker.lower() in report["reason"]
    assert report["landing_tip"] is None
    assert _steps(report) == ["freeze-probe"]
    assert _master_tip(repo) == pre


# --- post-verify: the skip-the-sync-leg class is un-writable ------------------


@pytest.mark.asyncio
async def test_reset_leg_failure_fails_the_report_loudly(tmp_path):
    """AC: the #353 skip-the-sync-leg class is un-writable. When the
    reset leg cannot run, post-verify fails the report loudly - there is
    no code path that reports a landing as ok with an unsynced primary."""
    repo = _repo_with_commit(tmp_path)
    _setup_run_branch(repo)
    _advance_master(repo)

    orig = gitexec.run_git

    async def broken_reset(workspace, *args, timeout=None, env=None):
        if args[:2] == ("reset", "--hard"):
            return 1, "simulated: reset could not run"
        return await orig(workspace, *args, timeout=timeout, env=env)

    gitexec.run_git = broken_reset
    try:
        report = await _land(repo)
    finally:
        gitexec.run_git = orig

    assert not report["ok"]
    assert _step(report, "reset")["ok"] is False
    assert _step(report, "post-verify")["ok"] is False
    assert "post-verify failed" in report["reason"]


@pytest.mark.asyncio
async def test_tracked_dirt_after_reset_fails_post_verify(tmp_path):
    """The clean-tree half of post-verify: the reset ran, but tracked
    dirt appears afterwards - the report fails loudly even though HEAD
    matches. (The #353 tangle - ref moved, primary never synced - is the
    same loud failure: post-verify refuses ok unless every leg ran.)"""
    repo = _repo_with_commit(tmp_path)
    _setup_run_branch(repo)
    _advance_master(repo)

    orig = gitexec.run_git

    async def dirty_after_reset(workspace, *args, timeout=None, env=None):
        out = await orig(workspace, *args, timeout=timeout, env=env)
        if args[:1] == ("reset",) and out[0] == 0:
            # simulate post-reset dirt appearing out of band
            _write(repo, "hello.txt", "dirt after the reset\n")
        return out

    gitexec.run_git = dirty_after_reset
    try:
        report = await _land(repo)
    finally:
        gitexec.run_git = orig

    assert not report["ok"]
    assert _step(report, "post-verify")["ok"] is False
    assert "tracked dirt" in _step(report, "post-verify")["detail"]


# --- cancellation -------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_during_merge_leaves_the_ref_unmoved(tmp_path):
    """Cancelled BEFORE the point-of-no-return: the landing aborts, the
    target ref never moves, and the landing lock is released (a second
    landing works)."""
    repo = _repo_with_commit(tmp_path)
    _setup_run_branch(repo)
    _advance_master(repo)
    pre = _master_tip(repo)

    started, release = asyncio.Event(), asyncio.Event()
    orig = gitexec.run_git

    async def gated_merge(workspace, *args, timeout=None, env=None):
        if args[:1] == ("merge-tree",):
            started.set()
            await release.wait()
        return await orig(workspace, *args, timeout=timeout, env=env)

    gitexec.run_git = gated_merge
    lock = asyncio.Lock()
    try:
        task = asyncio.create_task(_land(repo, lock=lock))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        gitexec.run_git = orig

    assert _master_tip(repo) == pre  # the ref never moved

    # the lock was released by the cancellation - a second landing runs
    report = await _land(repo, lock=lock)
    assert report["ok"], report


@pytest.mark.asyncio
async def test_cancel_inside_point_of_no_return_completes_the_landing(tmp_path):
    """Cancelled DURING the sync leg: the #357 absorb-then-finish shape
    lets the region complete (ref move + reset + post-verify) and only
    then re-raises - a cancelled landing is never a half-applied one."""
    repo = _repo_with_commit(tmp_path)
    _setup_run_branch(repo)
    _advance_master(repo)

    started, release = asyncio.Event(), asyncio.Event()
    orig = gitexec.run_git

    async def gated_reset(workspace, *args, timeout=None, env=None):
        if args[:2] == ("reset", "--hard"):
            started.set()
            await release.wait()
        return await orig(workspace, *args, timeout=timeout, env=env)

    gitexec.run_git = gated_reset
    try:
        task = asyncio.create_task(_land(repo))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()  # the Stop press arrives mid-landing
        release.set()  # ...the shielded region still completes
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        gitexec.run_git = orig

    # the landing fully applied despite the cancellation
    assert _git(repo, "cat-file", "-p", "master:hello.txt") == "run version"
    parents = _git(repo, "log", "-1", "--format=%P").split()
    assert len(parents) == 2
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""
