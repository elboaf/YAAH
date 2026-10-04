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
(``run/chat-<id>``) are what carry work. Master only ever moves by
human merge; the harness never moves it (ADR-0010, landing contract).

Kept out of the tool layer on purpose: this is app state, not a model
tool. Remote (``remote:``) workspaces are out of scope v1 (ADR-0010),
matching the existing remote skips in loop injection and the git
endpoints; non-git and Default (home) workspaces have no branch to
attach to and are skipped the same way.
"""
from pathlib import Path

from backend.agent.gitinfo import _run_git
from backend.agent.tools import workspace_root


def chat_worktree_path(workspace: str | None, chat_id: int | str) -> Path | None:
    """The deterministic per-chat worktree path, WITHOUT creating it.

    None when the chat cannot have one (no workspace, remote namespace).
    Existence is a separate question - see ensure_chat_worktree.
    """
    ws = (workspace or "").strip()
    if not ws or ws.startswith("remote:") or ws == ".":
        return None
    return workspace_root(ws) / ".scratch" / f"chat-{chat_id}"


async def ensure_chat_worktree(
    workspace: str | None, chat_id: int | str, branch: str | None
) -> dict:
    """Materialize the chat's worktree at its selected branch, once.

    Returns {"path": Path, "detached": bool, "created": bool} on success
    or {"path": None, "error": str} when the chat cannot have one (no
    local git workspace, unknown start point). Idempotent: an existing
    worktree is reported as-is, never recreated.
    """
    chat_dir = chat_worktree_path(workspace, chat_id)
    if chat_dir is None or not branch or not str(branch).strip():
        return {"path": None, "error": "no chat worktree"}
    branch = str(branch).strip()

    if chat_dir.exists():
        return {"path": chat_dir, "detached": _is_detached(chat_dir), "created": False}

    root = workspace_root(workspace)
    # Start at the selected branch itself; when git refuses (the branch is
    # checked out in the primary or another worktree - the common case for
    # master), detach at the same tip. One checkout per branch is a git
    # constraint, not a policy: the tip is identical either way.
    rc, out = await _run_git(root, "worktree", "add", str(chat_dir), branch)
    detached = False
    if rc != 0:
        rc, out2 = await _run_git(root, "worktree", "add", "--detach", str(chat_dir), branch)
        if rc != 0:
            return {"path": None, "error": (out2 or out).strip() or "worktree add failed"}
        detached = True
    return {"path": chat_dir, "detached": detached, "created": True}


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
