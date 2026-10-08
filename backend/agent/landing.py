"""The verified landing module (#354, ADR-0016): the ADR-0014 safe-sync
contract executes as code instead of prompt prose.

One interface, ``land(target) -> LandingReport`` (a plain serializable
dict, the wt_sweep report convention). The executor runs the ticket's
steps in order and any violation fails loudly in the report:

  1. freeze probe - a primary in an in-progress merge / rebase /
     cherry-pick refuses as manual-only (ADR-0015 section 4 semantics;
     ``rev-parse --git-path`` so linked-worktree layouts resolve; the
     resolved path is joined to the root because git prints it relative
     to the primary when the repo is not linked).
  2. scoped wip sweep - checkout-free plumbing: tracked modifications
     and already-staged paths are snapshotted against a copied temp
     index (``update-index --add --remove`` + ``write-tree``), committed
     onto ``refs/heads/wip/<slug>-<id>`` with the pre-landing tip as
     parent. NO ``add -A`` in the normal path: untracked noise stays
     untracked and survives the closing ``reset --hard``, so nothing is
     lost and no wip content is invented (ADR-0016 section 4). Unmerged
     index entries are refused earlier, at the freeze probe (a
     half-finished conflict resolution: staging it would resolve only
     the temp copy while the human's real index stays conflicted); any
     status record the parser does not fully recognize falls back to
     the wide ADR-0015 section 2 sweep - a WIDER snapshot, never a
     narrower one.
  3. plumbing merge - ancestry gates first (run contained => no-op
     landing at the target tip; target behind => fast-forward to the
     run tip), then ``merge-tree --write-tree --merge-base=<run base>
     <target tip> <run tip>``; on conflict replayed with ``-X theirs``
     so the RUN branch's side wins (ADR-0015 section 3; the target is
     branch 1 / ours, the run branch is branch 2 / theirs -
     ``theirs`` resolves to branch 2, pinned empirically on git
     2.53.0.windows.2). Diverged histories ALWAYS produce a real
     two-parent ``commit-tree`` landing commit - never a tree-equality
     "fast-forward" label, which for rewritten histories would be a
     lineage lie the freshness gate would refuse.
  4. freshness re-check + ref move + reset + post-verify run together
     under an in-process ``asyncio.Lock`` (one per backend process -
     ADR-0016 section 3 rejects a repo-level lock) AND under a bounded
     shield: a Stop-press cancellation is absorbed for the duration so
     a landing can never be cancelled *between* the ref move and the
     reset (the #353 lineage-replacement class); the cancellation is
     re-raised once the region has completed. Under the lock the target
     tip is re-verified with ``merge-base --is-ancestor``; a target
     that moved out from under the merge (out-of-band human landing -
     the accepted residual risk) triggers a re-merge from the new tip,
     never a lineage replacement. The ref move is CAS
     (``update-ref <ref> <new> <verified old>``), never ``branch -f``.
  5. post-verify - ``HEAD == landing tip`` and the primary's TRACKED
     state clean (``status --porcelain --untracked-files=no``).
     Untracked files are deliberately not a failure: they survive the
     reset by design (ADR-0016 section 4). Any violation fails the
     report with the evidence and the wip-branch restore command.

Git I/O goes through the gitexec seam (``run_git``); merge/plumbing
legs widen the read-poll timeout and the temp-index sweep rides a
merged env (GIT_INDEX_FILE).

Fossil probe (#355, ADR-0016 section 5): ``fossil_probe(primary) ->
Verdict`` answers "is the primary's staged index live WIP or a landing
fossil?" in one command. The index tree (``git write-tree``, read-only
over the real index) is compared to the HEAD tree; when they differ,
each differing blob is traced to the repo's refs (local branches -
which include ``wip/`` and ``run/`` names - plus tags). All blobs
reachable => ``fossil-candidate`` (the staged content is already
committed somewhere: a landing leftover); any blob reachable from no
ref => ``live-wip`` (content that exists only because someone typed it
into the index: do NOT reset it away). The verdict is a serializable
dict usable by any chat. One-command invocation: ``python -m
backend.agent.landing <workspace>`` (prints the JSON verdict) - see
``docs/agents/fossil-probe.md``.

This module covers the LOCAL safe-sync case only - target checked out
in the primary worktree. Remote/gateway and tool wiring are the
follow-up ticket (#356).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile

from backend.agent import gitexec

log = logging.getLogger(__name__)

# One landing lock per backend process (ADR-0016 section 3): all agent
# landings flow through the one server process, so the in-process lock
# serializes them. Tests can inject their own lock via land(lock=...).
_LANDING_LOCK = asyncio.Lock()

# The freshness gate re-reads the target under the lock and re-merges
# when it moved; a target that moves on EVERY re-read gives up without
# a ref move (bounded, and the CAS below would refuse the stale move
# anyway).
_MAX_FRESHNESS_ATTEMPTS = 3

_EVIDENCE_MAX = 1000


def _evidence(out: str | None) -> str:
    """Truncated command output carried in a step's evidence."""
    out = (out or "").strip()
    if len(out) > _EVIDENCE_MAX:
        return out[:_EVIDENCE_MAX] + " ...[truncated]"
    return out


def _step(name: str, ok: bool, detail: str, evidence: str = "") -> dict:
    """One serializable step record (the wt_sweep report convention)."""
    return {"step": name, "ok": ok, "detail": detail, "evidence": evidence}


def _failed(steps: list[dict]) -> str | None:
    for s in steps:
        if not s["ok"]:
            return s["step"]
    return None


def _base_report(target: str, workspace: str, steps: list[dict]) -> dict:
    return {
        "ok": _failed(steps) is None,
        "manual_only": False,
        "target": target,
        "workspace": workspace,
        "landing_tip": None,
        "steps": steps,
        "failed_step": _failed(steps),
        "reason": None,
        "conflicts_resolved": [],
        "wip_branch": None,
        "wip_files": [],
        "restore_command": None,
    }


def _restore_fields(wip_branch: str | None, swept: list[str]) -> dict:
    if wip_branch and swept:
        return {
            "wip_branch": wip_branch,
            "wip_files": swept,
            "restore_command": f"git cherry-pick {wip_branch}",
        }
    return {"wip_branch": None, "wip_files": [], "restore_command": None}


# --- freeze probe ----------------------------------------------------------


async def _freeze_verdict(root: str) -> tuple[list[str], dict]:
    """Names of the in-progress-operation markers present in the primary,
    plus the probe step record. ``rev-parse --git-path`` resolves each
    marker's per-worktree path (plain ``<root>/.git/<name>`` checks miss
    linked-worktree layouts); the answer may be relative to the primary,
    so it is joined before the existence test."""
    checks: list[tuple[str, object]] = [
        ("MERGE_HEAD", os.path.exists),
        ("CHERRY_PICK_HEAD", os.path.exists),
        ("rebase-merge", os.path.isdir),
        ("rebase-apply", os.path.isdir),
    ]
    markers: list[str] = []
    evidence_parts: list[str] = []
    for name, exists in checks:
        rc, out = await gitexec.run_git(root, "rev-parse", "--git-path", name)
        if rc != 0:
            # not a repo at all (or git broken): the caller's rev-parse
            # legs will fail loudly right after - report data-only here
            return [], _step(
                "freeze-probe", False, "git-path probe failed", _evidence(out)
            )
        p = out.strip()
        if p and not os.path.isabs(p):
            p = os.path.join(root, p)
        if p and exists(p):
            markers.append(name)
            evidence_parts.append(f"{name} at {p}")
    # Unmerged index entries are the #353 half-finished-conflict
    # class: an unmerged path WITHOUT merge markers means a conflict
    # resolution was abandoned mid-way. Staging it "resolves" only the
    # temp-index copy - the human's real index stays conflicted, the
    # tree stays dirty, and post-verify can never pass. Refuse
    # (manual-only, the ADR-0015 section 4 semantics).
    rc, out = await gitexec.run_git(
        root, "status", "--porcelain=v2", "-z", timeout=30.0
    )
    if rc != 0:
        return [], _step(
            "freeze-probe", False, "status probe failed", _evidence(out)
        )
    for rec in (out or "").split("\x00"):
        rec = rec.strip()
        if not rec:
            continue
        unmerged = rec.startswith("u ") or (
            rec[:1] in ("1", "2") and "U" in rec.split(" ", 2)[1]
        )
        if unmerged:
            markers.append("unmerged-index")
            evidence_parts.append(f"unmerged index entry: {rec[:120]}")
            break
    return markers, _step(
        "freeze-probe",
        True,
        (
            f"in-progress state: {', '.join(markers)}"
            if markers
            else "no in-progress merge/rebase/cherry-pick"
        ),
        "; ".join(evidence_parts),
    )


# --- scoped wip sweep ------------------------------------------------------


def _parse_status_v2(st: str) -> tuple[list[str], list[str], bool]:
    """Parse ``status --porcelain=v2 -z`` into
    (dirty_paths, untracked_paths, fallback_to_wide).

    Dirty = tracked modification or staged entry (the scoped sweep's
    exact population); untracked noise is returned separately because
    the scoped sweep leaves it alone. Field counting uses maxsplit so
    an unexpected layout can only WIDEN what gets swept, never narrow
    it: any unmerged or unrecognized record sets fallback_to_wide and
    the caller sweeps the whole tree (``add -A``), which is a superset
    of anything the parse might have missed.
    """
    dirty: list[str] = []
    untracked: list[str] = []
    fields = st.split("\x00")
    i = 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if not rec:
            continue
        if rec.startswith("? "):
            untracked.append(rec[2:])
            continue
        if rec.startswith("1 "):
            # 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
            try:
                xy = rec.split(" ", 2)[1]
                path = rec.split(" ", 8)[8]
            except IndexError:
                return dirty, untracked, True
            if "U" in xy:
                # unmerged state rendered as a type-1 record (index-info
                # -created conflicts render this way): not stage-able
                # path-wise - the wide fallback owns it
                if path and path not in dirty:
                    dirty.append(path)
                return dirty, untracked, True
            if path and path not in dirty:
                dirty.append(path)
            continue
        if rec.startswith("2 "):
            # 2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <path>
            # NUL <origPath> - stage BOTH: the orig side is a real
            # index/worktree deletion the rename record folds away
            try:
                xy = rec.split(" ", 2)[1]
                path = rec.split(" ", 9)[9]
                orig = fields[i]
                i += 1
            except IndexError:
                return dirty, untracked, True
            for p in (path, orig):
                if p and p not in dirty:
                    dirty.append(p)
            if "U" in xy:
                return dirty, untracked, True
            continue
        if rec.startswith("u "):
            # unmerged index entries cannot be staged path-wise; the
            # caller falls back to the wide sweep over the whole tree.
            # The path still parses (u <XY> <sub> <m1> <m2> <m3> <mW>
            # <h1> <h2> <h3> <path>) and joins the dirty list, so the
            # report's swept list stays honest about what was captured.
            try:
                path = rec.split(" ", 10)[10]
            except IndexError:
                return dirty, untracked, True
            if path and path not in dirty:
                dirty.append(path)
            return dirty, untracked, True
        # unknown record shape: widen rather than guess
        return dirty, untracked, True
    return dirty, untracked, False


def _copy_index_sync(src_path: str, dst_path: str) -> None:
    """Byte-copy the primary's index to the temp copy (blocking I/O - the
    caller runs it via asyncio.to_thread so the event loop never stalls)."""
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        dst.write(src.read())


async def _wip_sweep(
    root: str,
    pre_target_tip: str,
    wip_branch: str,
    timeout: float,
) -> tuple[dict, list[str], list[str]]:
    """Snapshot the primary's tracked+staged WIP onto ``wip_branch``.

    Returns (step record, swept file list, untracked survivors). No-op
    when there is nothing to sweep (step ok, empty lists).
    """
    rc, st = await gitexec.run_git(
        root, "status", "--porcelain=v2", "-z", timeout=timeout
    )
    if rc != 0:
        return (
            _step("wip-sweep", False, "status --porcelain=v2 failed", _evidence(st)),
            [],
            [],
        )
    dirty, untracked, wide_fallback = _parse_status_v2(st or "")

    if not dirty and not wide_fallback:
        # nothing tracked/staged in flight: no sweep, no wip branch
        # (untracked noise needs no snapshot - it survives the reset)
        return (
            _step(
                "wip-sweep",
                True,
                "primary tracked state clean - no sweep needed"
                + (
                    f" ({len(untracked)} untracked file(s) left untracked)"
                    if untracked
                    else ""
                ),
            ),
            [],
            untracked,
        )

    # Copy the index; stage the sweep against the COPY so the human's
    # staged state is never disturbed.
    tmpdir = tempfile.mkdtemp(prefix="landing-idx-")
    try:
        tmp_index = os.path.join(tmpdir, "index.tmp")
        rc, idx_path = await gitexec.run_git(root, "rev-parse", "--git-path", "index")
        idx_path = idx_path.strip() if rc == 0 else ""
        if not idx_path:
            return (
                _step(
                    "wip-sweep", False, "index path resolve failed", _evidence(idx_path)
                ),
                [],
                untracked,
            )
        if not os.path.isabs(idx_path):
            idx_path = os.path.join(root, idx_path)
        await asyncio.to_thread(_copy_index_sync, idx_path, tmp_index)
        env = {"GIT_INDEX_FILE": tmp_index}

        if wide_fallback:
            # Wide fallback (ADR-0015 section 2): add -A stages untracked
            # files too, and staging an unmerged path resolves it. Wider
            # than the scoped norm - acceptable for a state the scoped
            # sweep cannot express; still checkout-free plumbing.
            rc, out = await gitexec.run_git(
                root, "add", "-A", "--", ".", env=env, timeout=timeout
            )
            if rc != 0:
                return (
                    _step(
                        "wip-sweep",
                        False,
                        "unmerged-index fallback sweep failed",
                        _evidence(out),
                    ),
                    [],
                    untracked,
                )
            staged_paths = list(dirty) + list(untracked)
        else:
            rc, out = await gitexec.run_git(
                root,
                "update-index",
                "--add",
                "--remove",
                "--",
                *dirty,
                env=env,
                timeout=timeout,
            )
            if rc != 0:
                return (
                    _step(
                        "wip-sweep", False, "temp-index staging failed", _evidence(out)
                    ),
                    [],
                    untracked,
                )
            staged_paths = list(dirty)

        rc, tree = await gitexec.run_git(root, "write-tree", env=env, timeout=timeout)
        if rc != 0:
            return (
                _step("wip-sweep", False, "write-tree failed", _evidence(tree)),
                [],
                untracked,
            )
        tree = tree.strip().splitlines()[0].strip()

        rc, head_tree = await gitexec.run_git(
            root, "rev-parse", f"{pre_target_tip}^{{tree}}", timeout=timeout
        )
        if rc == 0 and head_tree.strip() == tree:
            # staging changed nothing HEAD doesn't already have: nothing
            # real to sweep (defensive - status should not have flagged
            # these paths)
            return (
                _step(
                    "wip-sweep", True, "sweep staged to HEAD's own tree - no wip needed"
                ),
                [],
                untracked,
            )

        rc, wip_commit = await gitexec.run_git(
            root,
            "commit-tree",
            tree,
            "-p",
            pre_target_tip,
            "-m",
            f"wip sweep of the primary pre-landing (carried from {pre_target_tip[:12]})",
            timeout=timeout,
        )
        if rc != 0:
            return (
                _step("wip-sweep", False, "commit-tree failed", _evidence(wip_commit)),
                [],
                untracked,
            )
        wip_commit = wip_commit.strip()
        # "swept" becomes true only once the wip branch exists: a later
        # failure must not advertise a restore command for a branch that
        # was never created.
        swept = staged_paths
        rc, out = await gitexec.run_git(
            root, "update-ref", f"refs/heads/{wip_branch}", wip_commit, timeout=timeout
        )
        if rc != 0:
            return (
                _step(
                    "wip-sweep", False, "wip branch update-ref failed", _evidence(out)
                ),
                [],
                untracked,
            )
        return (
            _step(
                "wip-sweep",
                True,
                f"swept {len(swept)} path(s) to wip branch {wip_branch}"
                + (" (unmerged-index wide fallback)" if wide_fallback else ""),
                "swept: " + ", ".join(swept[:20]) + (" ..." if len(swept) > 20 else ""),
            ),
            swept,
            untracked,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --- plumbing merge --------------------------------------------------------


def _parse_conflict_files(out: str) -> list[str]:
    """Conflicted paths from the FIRST merge-tree pass: stage lines look
    like ``<mode> <oid> <stage>\\t<path>`` (stages 1/2/3); everything
    after them is free-form informational text (probe-pinned on git
    2.53) and is ignored - a path qualifies only with a 6-8 digit mode,
    a 40-hex oid and a 1/2/3 stage."""
    files: list[str] = []
    hexdigits = set("0123456789abcdef")
    for ln in out.splitlines():
        ln = ln.rstrip("\r")
        if "\t" not in ln:
            continue
        meta, path = ln.split("\t", 1)
        parts = meta.split(" ")
        if (
            len(parts) == 3
            and parts[0].isdigit()
            and len(parts[1]) == 40
            and set(parts[1]) <= hexdigits
            and parts[2] in ("1", "2", "3")
            and path
            and path not in files
        ):
            files.append(path)
    return files


async def _merge_once(
    root: str,
    pre_target_tip: str,
    run_tip: str,
    timeout: float,
) -> tuple[int, str, str, list[str]]:
    """One plumbing merge of the run branch into the target tip, with
    the ``-X theirs`` replay on conflict. Returns
    (rc, tree_oid, informational output, conflicted file list)."""
    rc, mb = await gitexec.run_git(
        root, "merge-base", pre_target_tip, run_tip, timeout=timeout
    )
    if rc != 0:
        return 1, "", f"merge-base failed: {_evidence(mb)}", []
    run_base = mb.strip().splitlines()[0].strip()

    rc, out = await gitexec.run_git(
        root,
        "merge-tree",
        "--write-tree",
        f"--merge-base={run_base}",
        pre_target_tip,
        run_tip,
        timeout=timeout,
    )
    if rc == 0:
        tree = out.strip().splitlines()[0].strip() if out.strip() else ""
        return 0, tree, out, []

    conflicts = _parse_conflict_files(out or "")
    # Replay with -X theirs: branch 1 (ours) is the TARGET tip, branch 2
    # (theirs) is the RUN tip - the run branch's side wins every
    # conflicted path (ADR-0015 section 3; probe-pinned on git 2.53).
    rc2, out2 = await gitexec.run_git(
        root,
        "merge-tree",
        "--write-tree",
        f"--merge-base={run_base}",
        "-X",
        "theirs",
        pre_target_tip,
        run_tip,
        timeout=timeout,
    )
    if rc2 != 0:
        return (
            1,
            "",
            f"merge-tree and -X theirs replay both failed: {_evidence(out2)}",
            conflicts,
        )
    tree = out2.strip().splitlines()[0].strip() if out2.strip() else ""
    return 0, tree, out or "", conflicts


async def _merge_leg(
    root: str, pre_target_tip: str, run_tip: str, timeout: float
) -> tuple[dict, str | None, list[str]]:
    """The full merge leg: classify the merge tree against both tips'
    trees (already-contained / fast-forward / real merge) and build the
    landing commit. Returns (step record, landing tip, conflicted files)."""
    # Ancestry gates BEFORE the merge: they decide which classifications
    # are even possible. Without them, a run branch that rewrote the
    # same content as master (same tree, diverged history) would
    # mis-classify as fast-forward - and then the freshness gate's
    # ancestor check could never pass (found by the behavioral test:
    # three futile re-merges, landing refused).
    rc, _ = await gitexec.run_git(
        root,
        "merge-base",
        "--is-ancestor",
        run_tip,
        pre_target_tip,
        timeout=timeout,
    )
    if rc == 0:
        # the run branch is already contained in the target
        return (
            _step(
                "merge",
                True,
                "run branch already contained in target - no-op landing",
            ),
            pre_target_tip,
            [],
        )
    if rc != 1:
        return (
            _step("merge", False, "ancestry probe failed", _evidence(str(rc))),
            None,
            [],
        )
    rc, _ = await gitexec.run_git(
        root,
        "merge-base",
        "--is-ancestor",
        pre_target_tip,
        run_tip,
        timeout=timeout,
    )
    if rc == 0:
        # the target is strictly behind: the run tip fast-forwards it
        return _step("merge", True, "fast-forward to the run branch tip"), run_tip, []
    if rc != 1:
        return (
            _step("merge", False, "ancestry probe failed", _evidence(str(rc))),
            None,
            [],
        )

    rc, tree, info, conflicts = await _merge_once(root, pre_target_tip, run_tip, timeout)
    if rc != 0:
        return (
            _step("merge", False, "plumbing merge failed", info),
            None,
            conflicts,
        )

    # The ancestry gates above have already settled the fast-forward /
    # already-contained cases; reaching here means diverged histories.
    # The landing is ALWAYS a two-parent merge commit - even when the
    # merged tree happens to equal one side's tree (a rewritten-history
    # run branch landing onto the same content), a fast-forward label
    # would be a lineage lie: the freshness gate demands the landing tip
    # actually contain both tips.

    msg = (
        f"land {run_tip[:12]} into the target pre-landing tip "
        f"{pre_target_tip[:12]} (verified landing)"
    )
    rc, landing = await gitexec.run_git(
        root,
        "commit-tree",
        tree,
        "-p",
        pre_target_tip,
        "-p",
        run_tip,
        "-m",
        msg,
        timeout=timeout,
    )
    if rc != 0:
        return (
            _step("merge", False, "landing commit-tree failed", _evidence(landing)),
            None,
            conflicts,
        )
    detail = (
        f"two-parent landing commit ({len(conflicts)} conflict(s) resolved run-wins)"
        if conflicts
        else "clean two-parent landing commit"
    )
    return (
        _step("merge", True, detail, info or ""),
        landing.strip(),
        conflicts,
    )


# --- the landing executor --------------------------------------------------


async def land(
    target: str,
    *,
    workspace: str,
    run_branch: str,
    chat_id: str | int | None = None,
    title: str | None = None,
    timeout: float = 60.0,
    lock: asyncio.Lock | None = None,
) -> dict:
    """Land ``run_branch`` into ``target`` (a branch checked out in the
    primary worktree at ``workspace``): the full safe-sync executor.

    Returns a serializable LandingReport; never raises for landing
    outcomes - failures land in the report. Cancellation: a caller
    cancelled after the merge leg still gets the point-of-no-return
    region (freshness -> update-ref -> reset -> post-verify) COMPLETED,
    then the CancelledError re-raised - a landing is never left
    half-applied (#353 class).
    """
    ref = target.removeprefix("refs/heads/")
    async with (lock or _LANDING_LOCK):
        return await _land_locked(
            ref,
            workspace=workspace,
            run_branch=run_branch,
            chat_id=chat_id,
            title=title,
            timeout=timeout,
        )


async def _land_locked(
    target: str,
    *,
    workspace: str,
    run_branch: str,
    chat_id: str | int | None,
    title: str | None,
    timeout: float,
) -> dict:
    steps: list[dict] = []

    # 1. freeze probe (manual-only on refusal)
    markers, probe_step = await _freeze_verdict(workspace)
    steps.append(probe_step)
    if markers:
        report = _base_report(target, workspace, steps)
        if markers == ["unmerged-index"]:
            reason = (
                "primary index holds unmerged entries (a conflict "
                "resolution was left half-finished) - manual-only per "
                "ADR-0015 section 4"
            )
        else:
            reason = (
                "primary is mid-"
                + "/".join(m.lower() for m in markers)
                + " (in-progress human operation) - manual-only per ADR-0015 section 4"
            )
        report.update(
            ok=False,
            manual_only=True,
            reason=reason,
        )
        return report
    if not probe_step["ok"]:
        return _base_report(target, workspace, steps)

    # tips
    rc, pre = await gitexec.run_git(
        workspace, "rev-parse", "--verify", f"refs/heads/{target}", timeout=timeout
    )
    rc2, run_tip = await gitexec.run_git(
        workspace, "rev-parse", "--verify", run_branch, timeout=timeout
    )
    pre_tip = pre.strip() if rc == 0 else ""
    run_tip = run_tip.strip() if rc2 == 0 else ""
    if rc != 0 or rc2 != 0:
        steps.append(
            _step(
                "tips",
                False,
                "target/run tip resolve failed",
                _evidence(pre if rc != 0 else run_tip),
            )
        )
        return _base_report(target, workspace, steps)
    steps.append(
        _step("tips", True, f"target at {pre_tip[:12]}, run branch at {run_tip[:12]}")
    )

    # 2. scoped wip sweep
    # Frozen copy: the branch-naming helper lived in loop.py until
    # #360 (ADR-0017 direction, spec #359) removed the SOP prompt machinery. landing.py is
    # itself scheduled for deletion (#362); this copy keeps it green
    # until then.
    import re as _re

    def _wip_branch_name(title, chat_id=None) -> str:
        cid = (
            str(chat_id).strip()
            if chat_id is not None and str(chat_id).strip()
            else "<id>"
        )
        slug = (
            _re.sub(r"[^a-z0-9]+", "-", str(title or "").strip().lower())
            .strip("-")[:40]
            .rstrip("-")
        )
        if not slug or slug == "new-task":
            return f"wip/chat-{cid}"
        return f"wip/{slug}-{cid}"

    wip_branch = _wip_branch_name(title, chat_id)
    sweep_step, swept, untracked = await _wip_sweep(
        workspace, pre_tip, wip_branch, timeout
    )
    steps.append(sweep_step)
    if not sweep_step["ok"]:
        report = _base_report(target, workspace, steps)
        report["reason"] = (
            "wip sweep failed - nothing was moved; the primary is untouched"
        )
        return report

    # 3. plumbing merge (merge-then-verify: recomputed under the lock if
    # the target moved)
    merge_step, landing_tip, conflicts = await _merge_leg(
        workspace, pre_tip, run_tip, timeout
    )
    steps.append(merge_step)
    if landing_tip is None:
        report = _base_report(target, workspace, steps)
        report.update(
            conflicts_resolved=conflicts,
            **_restore_fields(wip_branch, swept),
        )
        report["reason"] = "merge leg failed - the target ref was not moved"
        return report

    # 4+5+6+7. freshness -> update-ref (CAS) -> reset --hard -> post-verify,
    # shielded as one point-of-no-return unit
    return await _point_of_no_return(
        target,
        workspace=workspace,
        pre_tip=pre_tip,
        run_tip=run_tip,
        landing_tip=landing_tip,
        timeout=timeout,
        steps=steps,
        conflicts=conflicts,
        swept=swept,
        untracked=untracked,
        wip_branch=wip_branch,
    )


async def _point_of_no_return(
    target: str,
    *,
    workspace: str,
    pre_tip: str,
    run_tip: str,
    landing_tip: str,
    timeout: float,
    steps: list[dict],
    conflicts: list[str],
    swept: list[str],
    untracked: list[str],
    wip_branch: str,
) -> dict:
    """Freshness re-check, CAS ref move, reset --hard, post-verify -
    shielded so a Stop-press cancellation cannot split the ref move from
    the reset (the cancelled-landing-leaves-a-moved-ref class). The
    inner task is absorbed-and-completed on cancellation, then the
    caller's CancelledError is re-raised from outside the git region
    (the loop.py #357 pattern)."""

    async def _finish() -> dict:
        steps2 = list(steps)
        verified_tip = pre_tip
        landing = landing_tip
        for attempt in range(_MAX_FRESHNESS_ATTEMPTS):
            rc, cur = await gitexec.run_git(
                workspace,
                "rev-parse",
                "--verify",
                f"refs/heads/{target}",
                timeout=timeout,
            )
            if rc != 0:
                steps2.append(
                    _step("freshness", False, "target re-read failed", _evidence(cur))
                )
                return _final_report(
                    target, workspace, steps2, None, conflicts, swept, wip_branch,
                    reason="target re-read failed under the landing lock",
                )
            cur = cur.strip()
            rc, _ = await gitexec.run_git(
                workspace,
                "merge-base",
                "--is-ancestor",
                cur,
                landing,
                timeout=timeout,
            )
            if rc == 0:
                verified_tip = cur
                steps2.append(
                    _step(
                        "freshness",
                        True,
                        f"target tip {cur[:12]} is an ancestor of the landing tip"
                        + (" (re-verified under the lock)" if attempt else ""),
                    )
                )
                break
            if rc != 1:
                steps2.append(
                    _step("freshness", False, "is-ancestor probe failed", _evidence(cur))
                )
                return _final_report(
                    target, workspace, steps2, None, conflicts, swept, wip_branch,
                    reason="freshness probe failed under the landing lock",
                )
            # The target moved since the merge was computed. Re-merge
            # from the NEW tip - extend a lineage, never replace one -
            # unless attempts are exhausted, in which case give up with
            # the ref unmoved.
            if attempt == _MAX_FRESHNESS_ATTEMPTS - 1:
                steps2.append(
                    _step(
                        "freshness",
                        False,
                        "target kept moving under the lock - giving up without a ref move",
                    )
                )
                return _final_report(
                    target, workspace, steps2, None, conflicts, swept, wip_branch,
                    reason="target kept moving under the landing lock",
                )
            merge_step2, landing2, conflicts2 = await _merge_leg(
                workspace, cur, run_tip, timeout
            )
            steps2.append(
                _step(
                    "freshness-remerge",
                    merge_step2["ok"],
                    f"target moved to {cur[:12]} under the lock - merge recomputed"
                    + ("" if merge_step2["ok"] else "; re-merge failed"),
                    merge_step2.get("evidence", ""),
                )
            )
            if landing2 is None:
                return _final_report(
                    target, workspace, steps2, None, conflicts2 or conflicts, swept,
                    wip_branch,
                    reason="target moved under the lock and the re-merge failed",
                )
            landing = landing2

        # CAS ref move - the verified tip is the expected old value
        rc, out = await gitexec.run_git(
            workspace,
            "update-ref",
            f"refs/heads/{target}",
            landing,
            verified_tip,
            timeout=timeout,
        )
        if rc != 0:
            steps2.append(
                _step(
                    "update-ref",
                    False,
                    "CAS ref move failed (target moved or git refused)",
                    _evidence(out),
                )
            )
            return _final_report(
                target, workspace, steps2, None, conflicts, swept, wip_branch,
                reason="update-ref failed - the target ref was not moved",
            )
        steps2.append(
            _step(
                "update-ref",
                True,
                f"refs/heads/{target} -> {landing[:12]} (CAS over {verified_tip[:12]})",
            )
        )

        # 6. the sync leg - not a suggestion; the next line of the code
        rc, out = await gitexec.run_git(
            workspace, "reset", "--hard", target, timeout=timeout
        )
        reset_ok = rc == 0
        steps2.append(
            _step(
                "reset",
                reset_ok,
                "primary reset --hard to the landed target"
                if reset_ok
                else "reset --hard FAILED - the primary tree was not synced",
                _evidence(out),
            )
        )

        # 7. post-verify: HEAD == landing tip AND tracked state clean
        # (untracked noise survives the reset by design - ADR-0016
        # section 4 - so it is counted, not failed on)
        rc, head = await gitexec.run_git(workspace, "rev-parse", "HEAD", timeout=timeout)
        rc2, st = await gitexec.run_git(
            workspace,
            "status",
            "--porcelain",
            "--untracked-files=no",
            timeout=timeout,
        )
        head_ok = rc == 0 and head.strip() == landing
        clean_ok = rc2 == 0 and (st or "").strip() == ""
        pv_ok = head_ok and clean_ok and reset_ok
        evidence = []
        if not head_ok:
            evidence.append(
                f"HEAD is {(head or '').strip()[:12] or '?'} expected {landing[:12]}"
            )
        if not clean_ok:
            evidence.append("tracked dirt after reset: " + _evidence(st))
        if not reset_ok:
            evidence.append("reset leg failed; the primary may still hold swept WIP")
        steps2.append(
            _step(
                "post-verify",
                pv_ok,
                "primary HEAD == landing tip, tracked state clean"
                if pv_ok
                else "; ".join(evidence),
                f"untracked survivors: {len(untracked)}",
            )
        )
        return _final_report(
            target,
            workspace,
            steps2,
            landing,
            conflicts,
            swept,
            wip_branch,
            reason=None if pv_ok else "post-verify failed - the landing did not fully apply",
        )

    task = asyncio.ensure_future(_finish())
    try:
        ret = await asyncio.shield(task)
    except asyncio.CancelledError as exc:
        # The shield keeps the first delivery out of the git legs, but
        # asyncio re-delivers into the inner task at its next await -
        # absorb it (uncancel), let the region finish, then re-raise
        # from OUTSIDE the git region. A cancelled landing is never a
        # half-applied landing (#353 class); the report of a landing
        # that completed under cancellation is logged, and the caller's
        # unwinding still happens (the cancel is re-raised either way).
        task.uncancel()
        try:
            ret = await asyncio.shield(task)
        except BaseException:  # noqa: BLE001 - the cancel is the headline
            raise exc from None
        log.info(
            "landing completed despite cancellation: %s",
            ret.get("detail") or ret.get("reason"),
        )
        raise exc from None
    return ret


def _final_report(
    target: str,
    workspace: str,
    steps: list[dict],
    landing_tip: str | None,
    conflicts: list[str],
    swept: list[str],
    wip_branch: str | None,
    *,
    reason: str | None,
) -> dict:
    report = _base_report(target, workspace, steps)
    report.update(
        landing_tip=landing_tip,
        reason=reason,
        conflicts_resolved=conflicts,
        **_restore_fields(wip_branch, swept),
    )
    if report["ok"] and landing_tip:
        report["detail"] = (
            f"landed into {target}: {landing_tip[:12]}"
            + (f"; {len(conflicts)} conflict(s) resolved run-wins" if conflicts else "")
            + (
                f"; primary WIP on {wip_branch} (restore: git cherry-pick {wip_branch})"
                if swept
                else ""
            )
        )
    return report


# --- fossil probe (ADR-0016 section 5, #355) --------------------------------


def _verdict(v: str, workspace: str, detail: str, **extra) -> dict:
    """One serializable Verdict dict (the report convention)."""
    base = {"verdict": v, "workspace": workspace, "detail": detail}
    base.update(extra)
    return base


# gitexec.run_git may return None (no executor could run git at all); the
# probe maps that to an error verdict - data, never read as absence.
async def _git_or_none(workspace: str, *args: str, timeout: float = 60.0):
    return await gitexec.run_git(workspace, *args, timeout=timeout)


def _reachable_refs(root: str, timeout: float) -> list[str] | None:
    """The ref universe the probe traces blobs against: local branches
    (which include wip/ and run/ names) plus tags. None => git failed.
    Synchronous by design: a pure read that must not be awaited per-ref
    (the async legs below stay on the gitexec seam)."""
    import subprocess

    proc = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)"],
        cwd=root,
        capture_output=True,
        timeout=max(timeout, 5.0),
        check=False,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    if proc.returncode != 0:
        return None
    return [
        ln.strip()
        for ln in proc.stdout.decode("utf-8", "replace").splitlines()
        if ln.startswith(("refs/heads/", "refs/tags/"))
    ]


async def fossil_probe(workspace: str, *, timeout: float = 60.0) -> dict:
    """One-command index-provenance answer for the primary's staged index
    (ADR-0016 section 5, #355; the #353 nine-handoff diagnosis class).

    READ-ONLY: ``write-tree`` hashes the real index without touching it.
    Returns a serializable Verdict:

    - ``clean``            - index tree == HEAD tree (nothing staged
                             beyond HEAD)
    - ``fossil-candidate`` - every differing blob lives in some local
                             branch/tag: content already committed
                             somewhere (evidence: blob -> containing
                             refs) - a landing fossil shape
    - ``live-wip``         - at least one differing blob reachable from
                             NO ref: content typed only into the index;
                             resetting it away would lose real work
                             (evidence: the unreachable blobs)
    - ``error``            - git failed (not a repo, channel down) -
                             data, not absence; never read as ``clean``
    """
    rc, head_tree = await _git_or_none(
        workspace, "rev-parse", "HEAD^{tree}", timeout=timeout
    )
    if rc != 0:
        return _verdict(
            "error", workspace, "HEAD tree resolve failed", evidence=_evidence(head_tree)
        )
    head_tree = head_tree.strip().splitlines()[0].strip()

    rc, idx_tree = await _git_or_none(workspace, "write-tree", timeout=timeout)
    if rc != 0:
        return _verdict(
            "error",
            workspace,
            "write-tree failed (unmerged index?)",
            evidence=_evidence(idx_tree),
        )
    idx_tree = idx_tree.strip().splitlines()[0].strip()

    if idx_tree == head_tree:
        return _verdict(
            "clean",
            workspace,
            "index tree equals HEAD tree - nothing staged beyond HEAD",
            index_tree=idx_tree,
        )

    # Differing trees: enumerate changed blobs with diff-tree -r over the
    # two tree oids. New blobs are DESTINATION-side (index tree) entries;
    # a staged DELETION has no destination blob and is classified by tree
    # membership below.
    rc, dt = await _git_or_none(
        workspace, "diff-tree", "-r", head_tree, idx_tree, timeout=timeout
    )
    if rc != 0:
        return _verdict("error", workspace, "diff-tree failed", evidence=_evidence(dt))
    # diff-tree A B diffs A -> B: the DESTINATION side (dst oid) is the
    # index tree's content, the side the probe traces to refs.
    new_blobs: list[dict] = []
    hexdigits = set("0123456789abcdef")
    # Two tree oids produce NO commit-id header line (unlike diff-tree
    # over commits) - every line is a record.
    for ln in (dt or "").splitlines():
        ln = ln.rstrip("\r")
        if "\t" not in ln:
            continue
        meta, path = ln.split("\t", 1)
        parts = meta.split(" ")
        # <mode_src> <mode_dst> <oid_src> <oid_dst> <status>; the meta
        # side starts with a colon (':100644'), and a pure staged
        # DELETION has dst mode 000000 and an all-zeros dst oid - not a
        # traceable blob; it falls to the tree-membership check below.
        if (
            len(parts) == 5
            and parts[1] not in ("0", "000000")
            and len(parts[3]) == 40
            and set(parts[3]) <= hexdigits
            and set(parts[3]) != {"0"}
            and parts[3] != parts[2]
        ):
            new_blobs.append({"oid": parts[3], "path": path})

    refs = _reachable_refs(workspace, timeout)
    if refs is None:
        return _verdict(
            "error",
            workspace,
            "for-each-ref failed",
            index_tree=idx_tree,
            head_tree=head_tree,
        )

    # Bounded trace: one rev-list --objects --all over the ref universe
    # answers "reachable from any ref" in one round-trip; a per-ref walk
    # then builds the blob -> containing-refs evidence.
    rc, out = await _git_or_none(
        workspace, "rev-list", "--objects", "--all", timeout=timeout
    )
    if rc != 0:
        return _verdict(
            "error",
            workspace,
            "rev-list failed",
            index_tree=idx_tree,
            head_tree=head_tree,
        )
    reachable = {
        ln.split(" ", 1)[0].strip() for ln in (out or "").splitlines() if ln.strip()
    }

    blob_refs: dict[str, list[str]] = {}
    for ref in refs:
        rc, out = await _git_or_none(
            workspace, "rev-list", "--objects", ref, timeout=timeout
        )
        if rc != 0:
            continue
        oids = {
            ln.split(" ", 1)[0].strip() for ln in (out or "").splitlines() if ln.strip()
        }
        for b in new_blobs:
            if b["oid"] in oids:
                blob_refs.setdefault(b["oid"], []).append(ref)

    orphans = [b for b in new_blobs if b["oid"] not in reachable]
    if orphans:
        return _verdict(
            "live-wip",
            workspace,
            (
                f"{len(orphans)} of {len(new_blobs)} differing blob(s) reachable "
                "from no ref - the index holds content that exists nowhere "
                "else; do NOT reset it away without saving"
            ),
            index_tree=idx_tree,
            head_tree=head_tree,
            unreachable_blobs=[b["path"] + " " + b["oid"] for b in orphans],
        )
    if new_blobs:
        return _verdict(
            "fossil-candidate",
            workspace,
            (
                "all " + str(len(new_blobs)) + " differing blob(s) already committed "
                "in refs - the staged content looks like a landing fossil, "
                "not live WIP"
            ),
            index_tree=idx_tree,
            head_tree=head_tree,
            blob_evidence=[
                b["path"] + " in " + ", ".join(blob_refs.get(b["oid"], [])[:5])
                for b in new_blobs
            ],
        )
    # Trees differ but no new-blob line parsed: a staged DELETION (the
    # index tree is missing content HEAD has, and no destination blob
    # exists to trace). The removal is a fossil only if some ref's tree
    # equals the index tree exactly; otherwise it is uncommitted live WIP.
    for ref in refs:
        rc, ref_tree = await _git_or_none(
            workspace, "rev-parse", ref + "^{tree}", timeout=timeout
        )
        if rc == 0 and ref_tree.strip() == idx_tree:
            return _verdict(
                "fossil-candidate",
                workspace,
                "staged removal's exact tree is committed on " + ref,
                index_tree=idx_tree,
                head_tree=head_tree,
                blob_evidence=["(staged deletion) tree matches " + ref],
            )
    return _verdict(
        "live-wip",
        workspace,
        (
            "index tree differs from HEAD tree (a staged removal with no "
            "committed counterpart anywhere) - do NOT reset it away"
        ),
        index_tree=idx_tree,
        head_tree=head_tree,
    )


def _cli() -> int:
    """The one-command invocation for agent chats (no git expertise):
    ``python -m backend.agent.landing <workspace>`` - prints the JSON verdict."""
    import sys

    ws = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    print(json.dumps(asyncio.run(fossil_probe(ws)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
