"""Per-chat worktrees (issue #277, ADR-0010).

Each chat whose user picked a branch gets its own linked worktree at the
deterministic path ``<workspace>/.scratch/chat-<id>/``, materialized at
the first run (not on chat creation, not on branch pick: idle chats cost
nothing). Runs then execute inside the chat's private tree, so the
primary worktree - the human's checkout - is untouchable by agents by
construction, not by prompt discipline.

Geometry: git allows a branch to be checked out in only one worktree at
a time, and the selected branch is usually checked out in the primary.
A chat worktree therefore checks out the selected branch when git
allows it, and detaches at the branch's tip when it does not - the
commit is identical either way; the agent's scratch run worktrees
(``.scratch/chat-<id>/run``, branch ``run/<title-slug>-<chat-id>``
since #312, ``run/chat-<id>`` before) are what carry work. Master only
ever moves by human merge; the harness never moves it (ADR-0010,
landing contract).

Kept out of the tool layer on purpose: this is app state, not a model
tool. Remote (``remote:``) workspaces are out of scope v1 (ADR-0010),
matching the existing remote skips in loop injection and the git
endpoints; non-git and Default (home) workspaces have no branch to
attach to and are skipped the same way.
"""
import logging
import time
from pathlib import Path

from backend.agent.gitinfo import _run_git
from backend.agent.tools import workspace_root

log = logging.getLogger("yaah.worktrees")


def chat_worktree_path(workspace: str | None, chat_id: int | str) -> Path | None:
    """The deterministic per-chat worktree path, WITHOUT creating it.

    None when the chat cannot have one (no workspace, remote namespace).
    Existence is a separate question - see ensure_chat_worktree.
    """
    ws = (workspace or "").strip()
    if not ws or ws.startswith("remote:") or ws == ".":
        return None
    return workspace_root(ws) / ".scratch" / f"chat-{chat_id}"


async def _ensure_scratch_invisible(workspace: str | None) -> None:
    """Keep ``<workspace>/.scratch/`` invisible to git (#321, ADR-0010).

    The whole per-chat namespace is welded to the deterministic
    ``.scratch/chat-<id>/`` path - the residue sweeper (``wt_sweep``) and
    ``runwatch`` both find worktrees only by that path shape - so a
    workspace whose own ``.gitignore`` does not cover ``.scratch/`` shows
    it as untracked noise and tempts agents into relocating worktrees,
    which silently breaks all of that. Invisibility is established via
    the UNTRACKED ``.git/info/exclude`` - never the user's tracked
    ``.gitignore``: no user file is modified, nothing is committed, and
    the rule is per-clone by design.

    Best-effort by contract: any probe or write failure logs a warning
    and returns - the worktree is still created, degrading to the
    pre-#321 behavior instead of blocking a run.
    """
    ws = (workspace or "").strip()
    if not ws or ws.startswith("remote:") or ws == ".":
        return  # no local git workspace: nothing to keep invisible
    root = workspace_root(ws)
    rc, _ = await _run_git(root, "check-ignore", "-q", ".scratch/")
    if rc == 0:
        return  # already ignored (rule, tracked .gitignore, or exclude)
    # rc == 1: not ignored -> the rule is missing. rc >= 2 (or 127, no
    # git): git could not answer at all - fall through, one warning below.
    rc2, out = await _run_git(root, "rev-parse", "--git-path", "info/exclude")
    exclude = out.strip() if rc2 == 0 and out.strip() else None
    if not exclude:
        log.warning(
            "scratch-invisible: cannot resolve info/exclude in %s; "
            ".scratch/ may show as untracked",
            root,
        )
        return
    try:
        exclude_path = Path(exclude)
        if not exclude_path.is_absolute():
            exclude_path = root / exclude_path
        lines = (
            exclude_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if exclude_path.exists()
            else []
        )
        if any(line.strip() == ".scratch/" for line in lines):
            return  # already listed: idempotent even if check-ignore disagreed
        lines = [line for line in lines if line.strip()]
        lines.append("# added by YAAH (#321): per-chat worktree namespace stays untracked")
        lines.append(".scratch/")
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning(
            "scratch-invisible: could not update %s (%s); .scratch/ may "
            "show as untracked",
            exclude,
            exc,
        )


async def ensure_chat_worktree(
    workspace: str | None, chat_id: int | str, branch: str | None
) -> dict:
    """Materialize the chat's worktree at its selected branch, once.

    Returns {"path": Path, "detached": bool, "created": bool} on success
    or {"path": None, "error": str} when the chat cannot have one (no
    local git workspace, unknown start point). Never recreated; when the
    selection changed since the last run, an existing clean tree is
    retargeted in place (flip = checkout inside the chat worktree; the
    dirty-flip guard runs upstream in branch_select).
    """
    chat_dir = chat_worktree_path(workspace, chat_id)
    if chat_dir is None or not branch or not str(branch).strip():
        return {"path": None, "error": "no chat worktree"}
    branch = str(branch).strip()

    if chat_dir.exists():
        # #321: establish .scratch/ invisibility before anything else
        # touches the namespace (idempotent, best-effort).
        await _ensure_scratch_invisible(workspace)
        # The dirty-flip guard ran in the caller, so an existing tree may
        # be retargeted in place: flip = checkout inside the chat's own
        # worktree (ADR-0010, branch-selector decision). Attached trees
        # switch to the branch (git refuses if another worktree holds it
        # - then detach at its tip, the same one-checkout fallback as
        # creation); detached trees re-attach only if the branch is free.
        was_detached = _is_detached(chat_dir)
        rc, out = await _run_git(chat_dir, "checkout", "-q", branch)
        if rc == 0:
            return {"path": chat_dir, "detached": False, "created": False}
        rc, out2 = await _run_git(chat_dir, "checkout", "-q", "--detach", branch)
        if rc == 0:
            return {"path": chat_dir, "detached": True, "created": False}
        return {
            "path": chat_dir,
            "detached": was_detached,
            "created": False,
            "error": (out2 or out).strip() or "worktree flip failed",
        }

    root = workspace_root(workspace)
    # #321: make .scratch/ git-invisible BEFORE the first worktree add,
    # so the deterministic namespace starts life untracked-and-invisible.
    await _ensure_scratch_invisible(workspace)
    # Start at the selected branch itself; when git refuses (the branch is
    # checked out in the primary or another worktree - the common case for
    # master), detach at the same tip. One checkout per branch is a git
    # constraint, not a policy: the tip is identical either way.
    rc, out = await _run_git(root, "worktree", "add", str(chat_dir), branch)
    detached = False
    if rc != 0:
        # #331: an unborn HEAD (fresh `git init`, no commits) makes that
        # fail with 'invalid reference' - the repo is real, just empty.
        # The orphan fallback is UNBORN-GATED (rev-parse --verify HEAD
        # fails): on a born repo git would happily accept --orphan -b for
        # a not-yet-existing branch name and silently hand back an empty
        # history-less worktree, masking whatever the plain add actually
        # failed on. When the primary later makes the first commit on the
        # shared unborn branch, both HEADs lift onto it together.
        rc_probe, _ = await _run_git(
            root, "rev-parse", "--verify", "--quiet", "HEAD", merge_stderr=False
        )
        if rc_probe != 0:
            rc2, out2 = await _run_git(
                root, "worktree", "add", "--orphan", "-b", branch, str(chat_dir)
            )
            if rc2 == 0:
                return {"path": chat_dir, "detached": False, "created": True}
            # Old git without orphan inference: the specified clear error,
            # not a generic worktree failure (triage criterion 3).
            return {
                "path": None,
                "error": (
                    "workspace repo has no commits yet - make the first "
                    "commit before creating a chat worktree"
                    + (f": {(out2 or '').strip()}" if (out2 or '').strip() else "")
                ),
            }
        # Born repo: the plain add failed for a real reason (branch held by
        # the primary or another worktree - the common case for master).
        # Detach at the same tip: one checkout per branch is a git
        # constraint, not a policy; the tip is identical either way.
        rc, out2 = await _run_git(root, "worktree", "add", "--detach", str(chat_dir), branch)
        if rc != 0:
            return {"path": None, "error": (out2 or out).strip() or "worktree add failed"}
        detached = True
    return {"path": chat_dir, "detached": detached, "created": True}


def chat_worktree_has_run_tree(chat_dir: Path) -> bool:
    """True when the deterministic harness namespace under the chat tree
    still holds anything (#329, #330): the run worktree
    (``<chat>/run``) or a nested chat tree (``<chat>/.scratch``) — both
    git-invisible since #321, so neither shows in ``status --porcelain``.
    The same existence gate the sweeper pins in-flight/residue trees
    with; retirement must never remove a tree the sweeper would keep.
    """
    return (chat_dir / "run").exists() or (chat_dir / ".scratch").exists()


async def retire_chat_worktree(
    workspace: str | None, chat_id: int | str, min_age_seconds: int = 0
) -> dict:
    """Retire the chat's worktree after its work has landed (#329).

    The lifecycle contract: a worktree exists only while its work is in
    flight. When the work lands on the chat's selected branch, the run
    worktree and the chat worktree itself go away — unless the chat tree
    is dirty or still holds run residue (then it survives and surfaces
    via the residue protocol) or a run tree still exists inside it. The
    agent never removes the tree it stands in: this is harness code,
    called at run end, never agent SOP.

    ``min_age_seconds`` guards the rapid-turn case: only retire a tree
    that has sat untouched (mtime) that long — 0 retires immediately
    (the post-run hook's choice; the tree was just used deliberately).
    Best-effort like the sweeper: any failure logs and reports
    ``{"retired": False, "reason": ...}`` without raising.

    Freeing the branch checkout is the point (``git worktree remove``
    releases it), so a finished chat never holds `auto/nightly-build`
    hostage the way chat-381 did (#329 census).
    """
    chat_dir = chat_worktree_path(workspace, chat_id)
    if chat_dir is None or not chat_dir.exists():
        return {"retired": False, "reason": "no chat worktree"}
    try:
        if min_age_seconds:
            age = time.time() - chat_dir.stat().st_mtime
            if age < min_age_seconds:
                return {"retired": False, "reason": f"age {int(age)}s < {min_age_seconds}s"}
        rc, out = await _run_git(chat_dir, "status", "--porcelain")
        if rc != 0:
            return {"retired": False, "reason": (out or "status failed").strip()[:200]}
        if (out or "").strip():
            return {"retired": False, "reason": "dirty"}
        if chat_worktree_has_run_tree(chat_dir):
            return {"retired": False, "reason": "run residue present"}
        rc, out = await _run_git(
            workspace_root(workspace), "worktree", "remove", str(chat_dir)
        )
        if rc != 0:
            return {"retired": False, "reason": (out or "worktree remove failed").strip()[:200]}
        return {"retired": True, "path": str(chat_dir)}
    except OSError as exc:
        return {"retired": False, "reason": str(exc)[:200]}


def _is_detached(chat_dir: Path) -> bool:
    """True when the worktree's HEAD is detached (no refs/heads symref)."""
    head = chat_dir / ".git"
    if not head.is_file():
        return False  # not a linked-worktree .git file; treat as attached
    try:
        line = head.read_text(encoding="utf-8", errors="replace").strip()
        target = Path(line.removeprefix("gitdir:").strip())
        if not target.is_absolute():
            target = (chat_dir / target).resolve()
        return not (target / "HEAD").exists() or not (
            (target / "HEAD").read_text(encoding="utf-8", errors="replace")
            .strip()
            .startswith("ref: refs/heads/")
        )
    except OSError:
        return False
