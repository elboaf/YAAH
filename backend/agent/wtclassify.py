"""Placement classification for session worktree isolation (ADR 0003).

Extracted from worktrees.py (ADR 0007 ticket 2: tenant split). Pure
decision logic, no lifecycle state. The keep-lists below answer one
question only — does this tool call need a session worktree? — and fail
closed: an unknown workspace-mutating placement isolates.
"""

from __future__ import annotations

import re

# Keep placement separate from access-mode authorization: these tools don't
# write the selected workspace, even though some still require approval.
_NON_WORKSPACE_MUTATIONS = {
    "install_git", "sandbox_test", "sandbox_run", "sandbox_stop",
    "memory_save", "memory_delete", "mouse_move", "mouse_click",
    "mouse_drag", "mouse_scroll", "type_text", "press_key", "focus_window",
}
_PLACEMENT_READ_TOOLS = {
    "read_file", "search_files", "git_status", "git_diff",
    "web_search", "web_fetch", "view_image", "load_skill", "get_help",
    "memory_read", "search_conversation_history", "sandbox_status",
    "screenshot", "list_windows", "read_ui_tree", "wait",
}
_DIRECT_MAIN_TREE_TOOLS = {"git_merge_back"}
_CHILD_DIRECT_WRITERS = {"delete_file", "move_file", "git_add", "git_commit"}

# A shell call is only a writer when its command actually mutates. The
# classifier gates the first session binding: explicit target=main structured
# Git operations resolve main independently in their executor. Fail-closed:
# unknown workspace-mutating tools isolate.
_READONLY_GIT = {
    "status", "log", "diff", "show", "branch", "remote", "rev-parse",
    "tag", "fetch", "pull", "push",
}
_READONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "pwd", "rg", "grep", "find", "wc",
    "which", "where", "dir", "type", "echo",
}

_SHELL_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")


def _readonly_segment(seg: str) -> bool:
    """One shell pipeline stage: recognized read-only, or env/cd prefixes
    in front of one. Anything else (unknown binary, flags that could hide
    a write, subshells) fails closed."""
    tokens = seg.strip().split()
    while tokens:
        first = tokens[0]
        if first in ("env", "time") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", first):
            tokens = tokens[1:]
            continue
        if first == "cd":
            return False
        break
    if not tokens:
        return False
    if tokens[0] == "git":
        return len(tokens) > 1 and tokens[1] in _READONLY_GIT
    if tokens[0] in _READONLY_COMMANDS:
        # Version probes like `node --version` and bare `ls` are fine;
        # don't try to whitelist every flag combination of every tool.
        return True
    if len(tokens) == 2 and tokens[1] in ("--version", "-v", "--help", "-h"):
        return True
    return False


def _readonly_shell_command(command: str) -> bool:
    if ">" in command or "<" in command or "$(" in command or "`" in command:
        return False
    return all(
        _readonly_segment(seg) or not seg.strip()
        for seg in _SHELL_SPLIT_RE.split(command)
    )


def should_isolate(tool_name: str, args: dict, *, child: bool = False) -> bool:
    """Whether this call needs a session worktree.

    Tree placement is separate from access-mode authorization. Child callers
    pass ``child=True`` because a child must isolate before any workspace
    mutation, including direct file deletion and structured Git changes. Git
    sync remains in the selected tree for parents; unknown placements fail
    closed.
    """
    if tool_name in (
        _NON_WORKSPACE_MUTATIONS | _PLACEMENT_READ_TOOLS | _DIRECT_MAIN_TREE_TOOLS
    ):
        return False
    if tool_name in {"git_pull", "git_push"}:
        # Primary-tree sync operations need no session binding. For current-tree
        # sync, child callers first isolate the inherited tree.
        return child and (args or {}).get("target", "current") == "current"
    if tool_name == "bash":
        command = str((args or {}).get("command") or "").strip()
        return not command or not _readonly_shell_command(command)
    # Direct file/git mutations don't independently trigger a parent bind,
    # but a sub-agent needs its own tree before invoking them.
    if child and tool_name in _CHILD_DIRECT_WRITERS:
        return True
    # Callers invoke this for tools they already permit to affect the
    # workspace; unknown placements default to isolation.
    return True
