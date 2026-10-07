"""Chat worktree lifecycle for remote chats (issue #334, spec #332;
ADR-0010 remote-parity amendment).

The remote twin of ``worktrees.py``: a chat aimed at a remote workspace
(``remote:<host>:<path>``) gets its chat worktree MATERIALIZED ON THE
HOST, in a client-owned namespace one level inside the git-invisible
``.scratch/`` tree:

    <host-repo>/.scratch/remote/chat-<id>/          (the chat worktree)
    <host-repo>/.scratch/remote/chat-<id>/run/      (the run worktree, agent SOP)

Placement is decided by two guests the host instance must not see:

- The host's own sweeper (``wt_sweep``) discovers per-chat trees by a
  ONE-LEVEL ``.scratch/chat-*`` glob in the workspace root. This module's
  namespace sits at ``.scratch/remote/chat-<id>`` — two levels down and
  under a ``remote`` dir the glob never descends into — so the host
  residue protocol can never retire a guest's work in flight (spec user
  story 15). A placement test pins this from the sweeper's side.
- The host's ``git status``: every path component is covered by the
  ``.scratch/`` exclude rule (#321) — ``.scratch/`` matches at any depth
  in git's pattern semantics — and the client WRITES that rule itself
  through the channel (``_ensure_scratch_invisible_remote``), never
  trusting the host to have it.

Lifecycle mirrors the local contract (ADR-0010, #329):

- Materialize at the chat's selected branch on the first run (attached
  when git allows, detached at the tip when the branch is checked out in
  another worktree — the same one-checkout-per-branch fallback).
- Land by plain merge onto the selected branch INSIDE the chat tree
  (the agent's run SOP); retire at run end when landed-clean; dirty or
  residue-holding trees survive and surface (#329 semantics).
- Every git operation rides the #333 gateway (``gitexec.run_git``);
  materialization failure over the channel returns an explicit error
  dict and the run proceeds under the universal degraded-mode rule
  (#322) — never a crash, never an improvised landing rule.

Legacy remote chats — whose only pin came from a creation-time pick —
materialize on their next run by the same code path: this module reads
the stored pin like the local one does; nothing is special anymore
(spec user story 8).

Empty-husk note: ``git worktree remove`` prunes only the tree's own
path, leaving any now-empty parent directories behind (observed on git
2.x, Windows; true locally and over the channel alike). The
deterministic namespace creates ``.scratch/remote/`` on the first add,
so after retirement those husks would stand forever and — worse — keep
the namespace probe reporting "residue" (the husk IS an ignored
``.scratch/`` dir). Retirement therefore prunes the empty parents
best-effort through the file tool (``delete_file`` rmdirs empty dirs
only — a dir holding anything real refuses and stays).
"""
from __future__ import annotations

import asyncio
import logging

from backend.agent import gitexec
from backend.agent.remote import parse_ns

log = logging.getLogger("yaah.wt_remote")

# The channel rides the host's bash tool; a materialize/retire sequence
# is a handful of hops. One command = one hop, per the gateway contract;
# this cap only keeps a wedged gateway from stalling run teardown.
_REMOTE_TIMEOUT = 30.0

# The deterministic namespace under the HOST REPO (the channel's cwd is
# the workspace root, so composed commands use workspace-relative paths;
# forward slashes inside one arg are accepted by git on every platform
# and never touch a shell metacharacter).
NAMESPACE_PARTS = (".scratch", "remote")


def chat_worktree_rel(chat_id: int | str) -> str:
    """The chat worktree's path RELATIVE to the host repo root,
    forward-slashed (cross-dialect safe: no quoting, no separators the
    shells disagree on)."""
    return "/".join(NAMESPACE_PARTS + (f"chat-{chat_id}",))


def _abs_host_path(host_path: str, chat_id: int | str) -> str:
    """The chat worktree's ABSOLUTE-ON-HOST path (for reports, never for
    composed commands — those take the relative form)."""
    sep = "\\" if "\\" in host_path else "/"
    return (
        host_path.rstrip("/\\")
        + sep
        + chat_worktree_rel(chat_id).replace("/", sep)
    )


def chat_worktree_path(workspace: str, chat_id: int | str) -> str | None:
    """The chat worktree's absolute-on-host path, from the workspace
    namespace. None when the workspace is not a remote namespace."""
    ns = parse_ns(workspace)
    if ns is None or not ns[1]:
        return None
    return _abs_host_path(ns[1], chat_id)


async def _run(ws: str, *args: str) -> tuple[int, str] | None:
    """One gateway hop under the module timeout. `ws` is the NAMESPACED
    workspace (the gateway routes remote execution on it). None = git
    could not run (host offline, channel error) — callers degrade
    explicitly."""
    try:
        return await asyncio.wait_for(
            gitexec.run_git(ws, *args), timeout=_REMOTE_TIMEOUT
        )
    except asyncio.TimeoutError:
        return None


# --------------------------------------------------------------- exclusion


async def _ensure_scratch_invisible_remote(ws: str) -> None:
    """Keep ``<host-repo>/.scratch/`` invisible to the HOST's git (#321
    rule, exercised through the channel — the exclude write goes over
    the wire, per the #334 placement criterion).

    Same semantics as the local ``worktrees._ensure_scratch_invisible``:
    check-ignore first (already covered by a rule, a tracked .gitignore,
    or the exclude file -> done); otherwise resolve info/exclude and
    append the rule through the file tools (the read_file/write_file
    executors run HOST-side with workspace-relative paths; write_file
    creates parent dirs, so a fresh repo without .git/info works too).

    Best-effort by contract: any failure logs a warning and returns —
    the worktree is still created (degrading to visible noise, the
    pre-#321 posture, instead of blocking the run).
    """
    from backend.agent.remote import parse_ns, remote_for_workspace

    session = remote_for_workspace(ws)
    if session is None:
        return
    host_path = (parse_ns(ws) or ("", ""))[1]
    res = await _run(ws, "check-ignore", "-q", ".scratch/")
    if res is None:
        # Host unreachable: materialization below hits the same wall and
        # degrades; nothing further to do here.
        log.warning(
            "scratch-invisible(remote): host unreachable; .scratch/ may "
            "show as untracked on the host"
        )
        return
    rc, _out = res
    if rc == 0:
        return  # already ignored (rule, tracked .gitignore, or exclude)
    rel_exclude = ".git/info/exclude"
    excl = await session.exec_tool(
        "read_file", {"path": rel_exclude}, workspace=ws
    )
    lines: list[str] = []
    if isinstance(excl, dict) and not excl.get("error"):
        # read_file numbers lines ("     1\ttext") for the model; strip
        # the gutter back off before rewriting the file verbatim.
        for line in str(excl.get("content") or "").splitlines():
            text = line.partition("\t")[2]
            lines.append(text if text else line)
    elif isinstance(excl, dict) and "File not found" in str(excl.get("error")):
        lines = []  # fresh repo without info/exclude: start the file
    else:
        log.warning(
            "scratch-invisible(remote): cannot read %s (%s); .scratch/ may "
            "show as untracked on the host",
            rel_exclude,
            excl.get("error") if isinstance(excl, dict) else excl,
        )
        return
    if any(line.strip() == ".scratch/" for line in lines):
        return  # already listed: idempotent even if check-ignore disagreed
    lines = [line for line in lines if line.strip()]
    lines.append("# added by YAAH (#321): per-chat worktree namespace stays untracked")
    lines.append(".scratch/")
    wrote = await session.exec_tool(
        "write_file",
        {"path": rel_exclude, "content": "\n".join(lines) + "\n"},
        workspace=ws,
    )
    if not isinstance(wrote, dict) or wrote.get("error"):
        log.warning(
            "scratch-invisible(remote): could not write %s (%s)",
            rel_exclude,
            wrote.get("error") if isinstance(wrote, dict) else wrote,
        )


# ---------------------------------------------------------- materialization


def _worktree_paths(porcelain: str) -> list[str]:
    """worktree path lines from `git worktree list --porcelain` output."""
    return [
        line[len("worktree "):].strip()
        for line in porcelain.splitlines()
        if line.startswith("worktree ")
    ]


async def ensure_chat_worktree(
    workspace: str, chat_id: int | str, branch: str | None
) -> dict:
    """Materialize the remote chat's worktree ON THE HOST at its selected
    branch, once. The remote twin of ``worktrees.ensure_chat_worktree`` —
    same return shapes:

    ``{"path": <abs host path>, "detached": bool, "created": bool}`` on
    success; ``{"path": None, "error": str}`` when the chat cannot have
    one (no remote workspace, no pin, host offline, not a repo, git
    refused). Existing trees flip in place (checkout inside the chat's
    own worktree — the dirty-flip guard runs upstream in the caller,
    exactly as local). Never raises.
    """
    ns = parse_ns(workspace)
    if ns is None or not ns[1]:
        return {"path": None, "error": "no remote git workspace"}
    host_path = ns[1]
    if not branch or not str(branch).strip():
        return {"path": None, "error": "no chat worktree"}
    branch = str(branch).strip()
    rel = chat_worktree_rel(chat_id)

    # Materialization must not trust the host's git hygiene: write the
    # exclude rule FIRST (#321 through the channel), then touch the tree.
    await _ensure_scratch_invisible_remote(workspace)

    # Existing tree (a previous run created it; a legacy chat's second
    # run arrives here too): find it in the worktree list — one hop,
    # text-parsed here; the channel cannot stat host paths without
    # shell quoting, which the cross-dialect contract forbids.
    wl = await _run(workspace, "worktree", "list", "--porcelain")
    if wl is not None and wl[0] == 0:
        for wt_path in _worktree_paths(wl[1]):
            if wt_path.replace("\\", "/").endswith(f"/{rel}"):
                return await _flip_existing(
                    workspace, host_path, rel, chat_id, branch
                )

    # Fresh materialization — the local flow, hop for hop: plain add,
    # then the UNBORN-GATED orphan fallback (#331 parity: an unborn
    # probe only runs after a failed add, so "not a repo" never reads
    # as "empty repo"), then the detached fallback (the branch is held
    # by another worktree — the common case for the host's primary).
    res = await _run(workspace, "worktree", "add", rel, branch)
    if res is None:
        return {"path": None, "error": "host unreachable"}
    rc, out = res
    if rc == 0:
        return {
            "path": _abs_host_path(host_path, chat_id),
            "detached": False,
            "created": True,
        }
    head = await _run(workspace, "rev-parse", "--verify", "--quiet", "HEAD")
    if head is None:
        return {"path": None, "error": "host unreachable"}
    if head[0] != 0:
        # Unborn HEAD (fresh `git init`, no commits): the repo is real,
        # just empty. Orphan add, gated exactly like local (#331).
        res2 = await _run(
            workspace, "worktree", "add", "--orphan", "-b", branch, rel
        )
        rc2, out2 = res2 or (1, "host unreachable")
        if rc2 == 0:
            return {
                "path": _abs_host_path(host_path, chat_id),
                "detached": False,
                "created": True,
            }
        return {
            "path": None,
            "error": (
                "workspace repo has no commits yet - make the first "
                "commit before creating a chat worktree"
                + (f": {(out2 or '').strip()}" if (out2 or "").strip() else "")
            ),
        }
    # Born repo: the plain add failed for a real reason (branch held by
    # another worktree) -> detach at the same tip.
    res2 = await _run(workspace, "worktree", "add", "--detach", rel, branch)
    rc2, out2 = res2 or (1, "host unreachable")
    if rc2 == 0:
        return {
            "path": _abs_host_path(host_path, chat_id),
            "detached": True,
            "created": True,
        }
    return {
        "path": None,
        "error": (out2 or out).strip() or "worktree add failed",
    }


async def _flip_existing(
    ws: str, host_path: str, rel: str, chat_id: int | str, branch: str
) -> dict:
    """Retarget an existing chat tree in place: checkout inside the chat's
    own worktree; on git's one-checkout refusal, detach at the branch's
    tip (the same fallback as creation). Mirrors the local flip."""
    probe = await _run(
        ws, "-C", rel, "rev-parse", "--abbrev-ref", "HEAD"
    )
    was_detached = bool(probe and probe[0] == 0 and probe[1] == "HEAD")
    res = await _run(ws, "-C", rel, "checkout", "-q", branch)
    rc, out = res or (1, "host unreachable")
    if rc == 0:
        return {
            "path": _abs_host_path(host_path, chat_id),
            "detached": False,
            "created": False,
        }
    res2 = await _run(
        ws, "-C", rel, "checkout", "-q", "--detach", branch
    )
    rc2, out2 = res2 or (1, "host unreachable")
    if rc2 == 0:
        return {
            "path": _abs_host_path(host_path, chat_id),
            "detached": True,
            "created": False,
        }
    return {
        "path": _abs_host_path(host_path, chat_id),
        "detached": was_detached,
        "created": False,
        "error": (out2 or out).strip() or "worktree flip failed",
    }


# --------------------------------------------------------------- retirement


async def _has_run_residue(ws: str, chat_rel: str, host_path: str) -> bool | None:
    """Remote twin of ``worktrees.chat_worktree_has_run_tree`` (#330):
    True while a nested RUN WORKTREE of this chat tree still stands.

    The run namespace is git-INVISIBLE (the ``.scratch/`` exclude rule
    matches at any depth), so plain ``status --porcelain`` cannot see it,
    and an ignored-file listing cannot be used either: after the agent's
    SOP landing (``git worktree remove <chat>/run``) git leaves the
    now-EMPTY parent dirs behind, and ``--ignored=matching`` would read
    that husk as residue forever, refusing every later retirement. The
    precise signal is the worktree REGISTRY: ``git worktree list
    --porcelain`` (one hop) — a standing run worktree is registered with
    an absolute path under the chat tree; an empty husk is not. Loose
    gitignored scratch files that are not a worktree are disposable
    scratch by definition and do not hold unlanded git work.

    True = residue stands (never retire); False = clean; None = git
    could not run (host unreachable — callers fail closed).
    """
    wl = await _run(ws, "worktree", "list", "--porcelain")
    if wl is None or wl[0] != 0:
        return None
    sep = "\\" if "\\" in host_path else "/"
    prefix = (host_path.rstrip("/\\") + sep + chat_rel.replace("/", sep)).replace(
        "\\", "/"
    ).rstrip("/").lower()
    for wt_path in _worktree_paths(wl[1]):
        norm = wt_path.replace("\\", "/").rstrip("/").lower()
        if norm.startswith(prefix + "/"):
            return True
    return False


async def retire_chat_worktree(workspace: str, chat_id: int | str) -> dict:
    """Retire the remote chat's worktree after its work has landed — the
    remote twin of ``worktrees.retire_chat_worktree`` (#329 semantics):
    clean + no residue -> remove; dirty or residue -> survive and
    surface via the residue protocol; unreachable -> refuse explicitly.
    Never raises. ``min_age`` is deliberately absent: the post-run hook
    is the only caller for remote chats (the sweeper cannot see this
    namespace by construction), and it retires immediately."""
    ns = parse_ns(workspace)
    if ns is None or not ns[1]:
        return {"retired": False, "reason": "no remote git workspace"}
    host_path = ns[1]
    rel = chat_worktree_rel(chat_id)

    st = await _run(workspace, "-C", rel, "status", "--porcelain")
    if st is None:
        return {"retired": False, "reason": "host unreachable"}
    if st[0] != 0:
        # Not a repo / tree never created / already gone — the same
        # nothing-to-retire report the local hook gives for a missing
        # tree. (A corrupt tree will resurface as a materialization
        # failure at the next run; retirement stays best-effort.)
        return {"retired": False, "reason": "no chat worktree"}
    if (st[1] or "").strip():
        return {"retired": False, "reason": "dirty"}

    residue = await _has_run_residue(workspace, rel, host_path)
    if residue is None:
        return {"retired": False, "reason": "host unreachable"}
    if residue:
        # A standing run worktree (or any nested namespace) keeps the
        # tree; the residue protocol surfaces it. The agent's SOP owns
        # removing its own run worktree — we only refuse to retire
        # under it, exactly like the local hook.
        return {"retired": False, "reason": "run residue present"}

    res = await _run(workspace, "worktree", "remove", rel)
    rc, out = res or (1, "host unreachable")
    if rc != 0:
        return {
            "retired": False,
            "reason": (out or "worktree remove failed").strip()[:200],
        }
    await _prune_empty_parents(workspace, chat_id)
    return {"retired": True, "path": _abs_host_path(host_path, chat_id)}


async def _prune_empty_parents(ws: str, chat_id: int | str) -> None:
    """Best-effort husk cleanup after a successful remove: git prunes
    only the chat tree's own path (plus, nowadays, empty ancestors it
    created), so ``.scratch/remote/`` can stand behind as ignored,
    EMPTY dirs. Nothing reads them as state — the residue probe keys on
    the worktree registry, not directory existence — so this is purely
    cosmetic namespace hygiene. delete_file rmdirs EMPTY dirs only, so
    every delete here no-ops safely when something real remains.
    Never raises; failures just leave the husk for the next run."""
    from backend.agent.remote import remote_for_workspace

    session = remote_for_workspace(ws)
    if session is None:
        return
    ns_prefix = "/".join(NAMESPACE_PARTS)
    cid = str(chat_id)
    for rel_dir in (
        f"{ns_prefix}/chat-{cid}/.scratch",  # innermost (run-less landing)
        f"{ns_prefix}/chat-{cid}",           # the tree's own husk
        ns_prefix,                           # if no other remote chat stands
        NAMESPACE_PARTS[0],                  # if the host has no .scratch use
    ):
        try:
            await session.exec_tool(
                "delete_file", {"path": rel_dir}, workspace=ws
            )
        except Exception:  # noqa: BLE001 - husk pruning never blocks
            return
