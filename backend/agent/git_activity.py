"""Per-run Git activity facts for the transcript summary.

Captures harness worktree lifecycle, structured Git tools, and conservative
single-command Git mutations. It does not claim to observe arbitrary scripts.
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_STATEFUL_TOOLS = {
    "git_add": "stage", "git_commit": "commit", "git_push": "push",
    "git_pull": "pull", "git_merge_back": "merge",
}
_SHELL_STATEFUL = {
    "add": "stage", "checkout": "checkout", "switch": "checkout",
    "commit": "commit", "merge": "merge", "push": "push", "pull": "pull",
    "rebase": "rebase", "reset": "reset", "stash": "stash", "clean": "clean",
    "restore": "restore", "rm": "remove", "mv": "move",
    "cherry-pick": "cherry-pick", "revert": "revert", "worktree": "worktree",
}
_SPLIT_COMMANDS = re.compile(r"&&|\|\||[;|\n]")


def _git_executable(token: str) -> bool:
    return Path(token.strip('"\'')).name.lower() in {"git", "git.exe"}


def _parse_git_command(command: str) -> tuple[str, list[str], bool] | None:
    """Recognize a single standalone state-changing Git command."""
    segments = [part.strip() for part in _SPLIT_COMMANDS.split(command) if part.strip()]
    if len(segments) != 1:
        return None
    simple = not any(marker in command for marker in ("$", "`", ">", "<", "(", ")"))
    try:
        tokens = shlex.split(segments[0], posix=True)
    except ValueError:
        return None
    if not tokens or not _git_executable(tokens[0]):
        return None
    args = tokens[1:]
    while args:
        if args[0] in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}:
            if len(args) < 2:
                return None
            args = args[2:]
        elif args[0] in {"--no-pager", "--literal-pathspecs", "--no-optional-locks"}:
            args = args[1:]
        else:
            break
    if not args:
        return None
    subcommand, rest = args[0].lower(), args[1:]
    operation = _SHELL_STATEFUL.get(subcommand)
    if operation is None:
        if subcommand == "branch" and (any(a in rest for a in ("-d", "-D", "-m", "-M", "-c", "-C")) or (rest and not rest[0].startswith("-"))):
            operation = "branch"
        elif subcommand == "tag" and rest and rest[0] not in {"-l", "--list", "--sort", "-v", "--verify"}:
            operation = "tag"
        else:
            return None
    if subcommand == "stash" and (not rest or rest[0] in {"list", "show"}):
        return None
    if subcommand == "worktree" and (not rest or rest[0] == "list"):
        return None
    if subcommand in {"checkout", "switch"}:
        positional = [a for a in rest if not a.startswith("-")][-1:]
    elif subcommand == "push":
        positional = [a for a in rest if not a.startswith("-")][:2]
    else:
        positional = [a for a in rest if not a.startswith("-")][:1]
    return operation, positional, simple


async def _run_git(workspace: str, *args: str) -> tuple[int, str]:
    from backend.agent.worktrees import _git_exe
    try:
        proc = await asyncio.create_subprocess_exec(
            _git_exe(), *args, cwd=workspace, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **({"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()
    except Exception:
        return 127, ""


async def _branch(workspace: str) -> str:
    rc, value = await _run_git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    return value if rc == 0 and value and value != "HEAD" else ""


async def _commit_head(workspace: str) -> dict[str, str]:
    rc, value = await _run_git(workspace, "show", "-s", "--format=%h%x1f%s", "HEAD")
    if rc != 0 or "\x1f" not in value:
        return {}
    sha, subject = value.split("\x1f", 1)
    return {"commit": sha[:12], "subject": subject[:180]}


async def _push_target(workspace: str, branch: str) -> tuple[str, str]:
    if not branch:
        return "", ""
    rc, upstream = await _run_git(workspace, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if rc == 0 and "/" in upstream:
        return tuple(upstream.split("/", 1))
    return "origin", branch


def _outcome(result: Any) -> str:
    if not isinstance(result, dict):
        return "unknown"
    error = _clean_detail(result.get("error") or result.get("reason"), 500).lower()
    if result.get("cancelled") or result.get("stopped") or "cancelled" in error or "stopped by user" in error:
        return "cancelled"
    if result.get("skipped"):
        return "skipped"
    if result.get("merged") is False and result.get("zero_commits"):
        return "no-op"
    if result.get("merged") is True or result.get("exit_code") == 0:
        return "succeeded"
    if any(word in error for word in ("refused", "denied", "blocked", "approval required")):
        return "blocked"
    if result.get("error") or result.get("reason") or result.get("exit_code") not in (None, 0):
        return "failed"
    return "unknown"


def _clean_detail(value: Any, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


@dataclass
class GitActivity:
    run_id: str
    lanes: dict[str, dict[str, Any]] = field(default_factory=dict)
    _sequence: int = 0

    def _lane(self, actor: str, label: str) -> dict[str, Any]:
        return self.lanes.setdefault(actor, {
            "id": actor, "label": label, "branch": "", "base_branch": "",
            "branch_action": "none", "commits_ahead": None, "dirty": None,
            "worktree": "none", "integrated": None, "operations": [],
        })

    def set_context(
        self, actor: str, label: str, *, branch: str = "", base_branch: str = "",
        branch_action: str = "none", worktree: str = "none",
    ) -> None:
        lane = self._lane(actor, label)
        if branch:
            lane["branch"] = branch
        if base_branch:
            lane["base_branch"] = base_branch
        if branch_action != "none":
            lane["branch_action"] = branch_action
        if worktree != "none":
            lane["worktree"] = worktree

    def set_settlement(
        self, actor: str, label: str, *, commits_ahead: int | None,
        dirty: bool | None, worktree: str, integrated: bool | None = None,
    ) -> None:
        lane = self._lane(actor, label)
        lane["commits_ahead"] = commits_ahead
        lane["dirty"] = dirty
        lane["worktree"] = worktree
        if integrated is True or lane.get("integrated") is not True:
            lane["integrated"] = integrated

    def record(
        self, actor: str, label: str, operation: str, outcome: str, *, source: str,
        detail: str = "", branch: str = "", remote: str = "", target_branch: str = "",
        commit: str = "", subject: str = "", certainty: str = "confirmed",
    ) -> None:
        lane = self._lane(actor, label)
        if branch:
            lane["branch"] = branch
        self._sequence += 1
        row: dict[str, Any] = {
            "sequence": self._sequence, "operation": operation, "outcome": outcome,
            "source": source, "certainty": certainty,
        }
        for key, value in (("detail", detail), ("branch", branch), ("remote", remote),
                           ("target_branch", target_branch), ("commit", commit), ("subject", subject)):
            if value:
                row[key] = _clean_detail(value)
        lane["operations"].append(row)

    async def record_tool(self, actor: str, label: str, tool: str, args: dict, result: Any, workspace: str) -> None:
        operation = _STATEFUL_TOOLS.get(tool)
        if not operation:
            return
        outcome = _outcome(result)
        branch = await _branch(workspace)
        detail = ""
        commit_data: dict[str, str] = {}
        remote = target_branch = ""
        if tool == "git_commit" and outcome == "succeeded":
            commit_data = await _commit_head(workspace)
        elif tool == "git_push" and outcome == "succeeded":
            remote, target_branch = await _push_target(workspace, branch)
            detail = f"published {branch} to {remote}/{target_branch}" if remote and target_branch else "published branch"
        elif tool == "git_push" and outcome != "succeeded":
            detail = _clean_detail((result or {}).get("error"))
        elif tool == "git_merge_back":
            source_branch = str((result or {}).get("branch") or args.get("branch") or "")
            target_branch = str((result or {}).get("target_branch") or "")
            branch = await _branch(workspace)
            detail = _clean_detail((result or {}).get("reason") or (result or {}).get("error") or (result or {}).get("note"))
            if outcome == "succeeded":
                self._lane(actor, label)["integrated"] = True
        elif tool in {"git_add", "git_pull"}:
            detail = _clean_detail((result or {}).get("output") or (result or {}).get("error"))
        if tool == "git_merge_back":
            self.record(
                actor, label, operation, outcome, source="agent-tool", detail=detail,
                branch=source_branch, target_branch=target_branch,
            )
        else:
            self.record(
                actor, label, operation, outcome, source="agent-tool", detail=detail,
                branch=branch, remote=remote, target_branch=target_branch,
                commit=commit_data.get("commit", ""), subject=commit_data.get("subject", ""),
            )

    async def record_shell(self, actor: str, label: str, command: str, result: Any, workspace: str) -> None:
        parsed = _parse_git_command(command)
        if not parsed:
            return
        operation, targets, exact_outcome = parsed
        outcome = _outcome(result)
        branch = await _branch(workspace)
        target = targets[0] if targets else ""
        detail = target
        commit_data: dict[str, str] = {}
        if operation == "commit" and outcome == "succeeded":
            commit_data = await _commit_head(workspace)
        remote = ""
        target_branch = ""
        if operation == "push":
            if len(targets) > 1:
                remote, target_branch = targets[0], targets[1]
            elif len(targets) == 1:
                remote, target_branch = targets[0], branch
            elif outcome == "succeeded":
                remote, target_branch = await _push_target(workspace, branch)
        elif operation in {"checkout", "branch", "merge", "cherry-pick", "revert"}:
            target_branch = target
        self.record(
            actor, label, operation, outcome, source="shell", detail=detail,
            branch=branch, remote=remote, target_branch=target_branch,
            commit=commit_data.get("commit", ""), subject=commit_data.get("subject", ""),
            certainty="confirmed" if exact_outcome and outcome in {"succeeded", "failed", "blocked"} else "uncertain",
        )

    def add_child(self, summary: dict | None, spawn_id: str) -> None:
        if not isinstance(summary, dict):
            return
        for child_lane in summary.get("lanes", []):
            if not isinstance(child_lane, dict):
                continue
            child_actor = str(child_lane.get("id") or "subagent")
            actor = f"sub:{spawn_id}:{child_actor}"
            label = _clean_detail(child_lane.get("label") or "Sub-agent", 80)
            lane = self._lane(actor, label)
            for key in ("branch", "base_branch", "branch_action", "commits_ahead", "dirty", "worktree", "integrated"):
                if child_lane.get(key) is not None:
                    lane[key] = child_lane[key]
            for op in child_lane.get("operations", []):
                if isinstance(op, dict):
                    copied = dict(op)
                    self._sequence += 1
                    copied["sequence"] = self._sequence
                    lane["operations"].append(copied)

    def summary(self, outcome: str) -> dict | None:
        lanes = [lane for lane in self.lanes.values() if lane.get("branch") or lane["operations"]]
        if not lanes:
            return None
        return {
            "version": 1, "run_id": self.run_id, "outcome": outcome,
            "captured_at": time.time(), "lanes": lanes,
            "coverage": "Harness worktree lifecycle, structured Git tools, and recognizable single-command shell Git mutations. Compound scripts and commands not matched by the conservative recognizer remain in the tool trace.",
        }


def record_worktree_lifecycle(activity: GitActivity, actor: str, label: str, info: dict) -> None:
    event = str(info.get("event") or "")
    branch = str(info.get("branch") or "")
    base_branch = str(info.get("base_branch") or "")
    if event in {"created", "reused", "inherited"}:
        activity.set_context(
            actor, label, branch=branch, base_branch=base_branch,
            branch_action="reused" if event == "inherited" else event, worktree="kept",
        )
        if event != "inherited":
            lane = activity._lane(actor, label)
            detail = "created isolated worktree" if event == "created" else "reused existing worktree"
            already_recorded = any(
                op.get("source") == "harness"
                and op.get("operation") == "checkout"
                and op.get("branch") == branch
                and op.get("detail") == detail
                for op in lane["operations"]
            )
            if not already_recorded:
                activity.record(
                    actor, label, "checkout", "succeeded", source="harness",
                    detail=detail, branch=branch,
                )
    elif event == "create_failed":
        activity.record(actor, label, "checkout", "failed", source="harness", detail=_clean_detail(info.get("reason")))
