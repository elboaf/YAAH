"""Desks — per-worker clone seats outside the repo (ADR-0022 developer world).

A desk is a seat directory ``~/.yaah/desks/<workspace>/<n>`` holding a clone
of the workspace repo whose only remote is the workspace repo itself (no
origin). One pool per workspace, created on demand, capped. Assignment is a
handover: fetch from the workspace repo and reset tracked files to the
workspace's current state — a predecessor's uncommitted edits do not survive —
while untracked files (node_modules, builds) and old branches carry over. Desk
directories outlive their worker chats; that inheritance is the point.

Desks live OUTSIDE the repo on purpose: ``legacy_residue.py`` only walks
registered workspace paths, so it never meets them (#364). Nothing here ever
registers a desk in the ``workspaces`` table.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from pathlib import Path

from backend.agent.gitproc import git_exe

log = logging.getLogger(__name__)

DESKS_ROOT = Path.home() / ".yaah" / "desks"
DEFAULT_POOL_CAP = 4
# Seat occupancy marker, INSIDE the seat's .git: untracked files in the
# worktree would be swallowed by a worker's `git add .` and deleted by the
# next checkout (the seat would read as free while occupied). Nothing git
# ever does — status, add, checkout, reset --hard, clean -fdx — touches
# files under .git.
DESK_MARKER = ".git/yaah-desk.json"

# Inbox rows (per workspace). In-memory with a JSON mirror per workspace so
# a backend restart drains, not drops. A row is pending from creation until
# status in {merged, rejected, abandoned, stale}; one pending per worker.
_INBOX: dict[str, list[dict]] = {}
_SEQ = [0]


def pool_key(workspace: str | None) -> str:
    """Stable per-workspace pool directory name: readable slug + short hash.

    The hash keeps two sibling directories (foo, Foo, foo-app) from sharing
    a pool on case-insensitive filesystems; the slug keeps the dir readable.
    """
    from backend.agent.tools import workspace_root

    ws = (workspace or "").strip()
    if not ws or ws == ".":
        resolved = Path.home().resolve()
        slug = "default"
    else:
        resolved = workspace_root(ws)
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", resolved.name).strip("-") or "ws"
    digest = hashlib.sha1(resolved.as_posix().encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def is_desk_dir(path) -> bool:
    """True when `path` resolves under ~/.yaah/desks/<pool>/<seat>."""
    try:
        resolved = Path(path).resolve()
    except Exception:  # noqa: BLE001
        return False
    if DESKS_ROOT not in resolved.parents and resolved != DESKS_ROOT:
        return False
    # The pool component and the seat component directly under it.
    try:
        rel = resolved.relative_to(DESKS_ROOT)
        parts = rel.parts
    except ValueError:
        return False
    return len(parts) >= 2 and parts[-1].isdigit()


def pool_dir(workspace: str | None) -> Path:
    return DESKS_ROOT / pool_key(workspace)


def inbox_path(workspace: str | None) -> Path:
    return pool_dir(workspace) / "inbox.json"


# ---- inbox persistence -----------------------------------------------------


def _load_inbox(workspace: str | None) -> list[dict]:
    key = pool_key(workspace)
    if key in _INBOX:
        return _INBOX[key]
    rows: list[dict] = []
    p = inbox_path(workspace)
    if p.is_file():
        try:
            rows = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = []
        except Exception:  # noqa: BLE001 — a corrupt mirror must not block boot
            log.warning("desk inbox mirror unreadable: %s", p)
    _INBOX[key] = rows
    return rows


def _save_inbox(workspace: str | None) -> None:
    p = inbox_path(workspace)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(_INBOX.get(pool_key(workspace), []), indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 — the memory copy stays authoritative
        log.warning("desk inbox mirror write failed: %s", p)


def pending_contribution(workspace: str | None, worker_conv_id: int) -> dict | None:
    """The worker's at-most-one pending contribution, or None."""
    for row in _load_inbox(workspace):
        if row.get("worker_conversation_id") == worker_conv_id and row.get(
            "status"
        ) == "pending":
            return row
    return None


def list_inbox(workspace: str | None, include_closed: bool = False) -> list[dict]:
    rows = list(_load_inbox(workspace))
    if not include_closed:
        rows = [r for r in rows if r.get("status") == "pending"]
    return rows


def add_contribution(
    workspace: str | None,
    worker_conversation_id: int,
    branch: str,
    base_commit: str,
    summary: str,
    desk_dir: str,
    title: str = "",
) -> dict:
    _SEQ[0] += 1
    row = {
        "id": _SEQ[0],
        "worker_conversation_id": worker_conversation_id,
        "branch": branch,
        "base_commit": base_commit,
        "summary": summary,
        "desk_dir": desk_dir,
        "title": title,
        "status": "pending",
        "created_at": "",
        "review": "",
    }
    _load_inbox(workspace).append(row)
    _save_inbox(workspace)
    return row


def update_contribution(workspace: str | None, contribution_id: int, **fields) -> dict | None:
    for row in _load_inbox(workspace):
        if row.get("id") == contribution_id:
            row.update(fields)
            _save_inbox(workspace)
            return row
    return None


# ---- pool ------------------------------------------------------------------


def desk_seat_dir(workspace: str | None, seat: int) -> Path:
    return pool_dir(workspace) / str(seat)


def _workspace_git_available() -> bool:
    return shutil.which("git", path=None) is not None or bool(git_exe())


async def _run_git(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """One plain git invocation (clone/fetch/reset plumbing). Never raises."""
    exe = git_exe() or "git"
    proc = await asyncio.create_subprocess_exec(
        exe, "--no-optional-locks", *args,
        cwd=str(cwd) if cwd is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace")


async def _head_commit(git_dir: Path) -> str:
    rc, out = await _run_git(["rev-parse", "HEAD"], cwd=git_dir)
    return out.strip() if rc == 0 else ""


async def _current_branch(git_dir: Path) -> str:
    rc, out = await _run_git(["branch", "--show-current"], cwd=git_dir)
    return out.strip() if rc == 0 else ""


async def _git_status_porcelain(git_dir: Path) -> str:
    rc, out = await _run_git(["status", "--porcelain"], cwd=git_dir)
    return out if rc == 0 else "?? __desk_status_error__"


async def _default_branch_of(source: Path) -> str:
    """The workspace checkout's current branch is the integration branch."""
    return await _current_branch(source) or "master"


async def create_clone(workspace: str | None, seat_dir: Path) -> None:
    """Clone the workspace repo into a fresh seat. The clone's only remote
    is the workspace repo; there is no origin (the overnight-agent sin is
    structurally impossible from a desk)."""
    from backend.agent.tools import workspace_root

    source = workspace_root(workspace)
    seat_dir.parent.mkdir(parents=True, exist_ok=True)
    rc, out = await _run_git(
        ["clone", "--origin", "workspace", str(source), str(seat_dir)]
    )
    if rc != 0:
        raise RuntimeError(f"desk clone failed: {out.strip()[:500]}")


async def handover(workspace: str | None, seat_dir: Path) -> dict:
    """Prepare a desk for its next occupant: fetch from the workspace repo
    and reset tracked files to the workspace's current state (its current
    branch + HEAD). Untracked files (node_modules, builds) and old branches
    survive; a predecessor's uncommitted edits do not."""
    from backend.agent.tools import workspace_root

    source = workspace_root(workspace)
    branch = await _default_branch_of(source)
    notes: list[str] = []
    rc, out = await _run_git(["fetch", "workspace", "--prune"], cwd=seat_dir)
    if rc != 0:
        notes.append(f"fetch failed: {out.strip()[:200]}")
    # The checkout's HEAD travels through the fetch; reset the seat's
    # current branch to it, hard — tracked files only, untracked survive.
    rc, out = await _run_git(
        ["reset", "--hard", "workspace/" + branch], cwd=seat_dir
    )
    if rc != 0:
        # Seat on an unrelated branch: move it to the workspace branch first.
        rc2, out2 = await _run_git(
            ["checkout", "-B", branch, "workspace/" + branch], cwd=seat_dir
        )
        if rc2 != 0:
            notes.append(f"reset failed: {out2.strip()[:200]}")
    else:
        await _run_git(["checkout", branch], cwd=seat_dir)
    # Drop branch leftover debris only if a stale rebase/merge state blocks
    # the next occupant; git refuses otherwise and the note carries it.
    return {"branch": branch, "notes": notes}


async def claim_desk(workspace: str | None) -> dict:
    """Claim a seat from the workspace's pool, cloning or handing over as
    needed. Returns the seat descriptor. Fails loudly at the pool cap."""
    from backend.agent.tools import workspace_root, is_git_workspace

    ws_root = workspace_root(workspace)
    if not is_git_workspace(ws_root):
        raise RuntimeError(
            "this workspace has no git checkout — desks clone git repos, "
            "so worker chats are unavailable here"
        )
    pool = pool_dir(workspace)
    pool.mkdir(parents=True, exist_ok=True)
    seats = sorted(
        (s for s in pool.iterdir() if s.is_dir() and s.name.isdigit()),
        key=lambda s: int(s.name),
    )
    for seat_dir in seats:
        marker = seat_dir / DESK_MARKER
        occupant = None
        if marker.is_file():
            try:
                occupant = json.loads(marker.read_text(encoding="utf-8")).get(
                    "worker_conversation_id"
                )
            except Exception:  # noqa: BLE001 — unreadable marker = unseated
                occupant = None
        if occupant:
            continue
        hand = await handover(workspace, seat_dir)
        await _write_marker(seat_dir, None)
        return {
            "seat": int(seat_dir.name),
            "dir": str(seat_dir),
            "branch": hand["branch"],
            "notes": hand["notes"],
            "reused": True,
        }
    cap = pool_cap()
    if len(seats) >= cap:
        raise RuntimeError(
            f"desk pool for this workspace is at its cap ({cap} seats) — "
            "no free desk to spawn into. Stop a finished worker or raise "
            "the cap (developer_world.pool_cap)."
        )
    seat = (int(seats[-1].name) + 1) if seats else 1
    seat_dir = desk_seat_dir(workspace, seat)
    await create_clone(workspace, seat_dir)
    branch = await _current_branch(seat_dir)
    await _write_marker(seat_dir, None)
    return {
        "seat": seat,
        "dir": str(seat_dir),
        "branch": branch,
        "notes": [],
        "reused": False,
    }


async def _write_marker(seat_dir: Path, worker_conversation_id: int | None) -> None:
    marker = seat_dir / DESK_MARKER
    data: dict = {"worker_conversation_id": worker_conversation_id}
    try:
        if marker.is_file():
            data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    data["worker_conversation_id"] = worker_conversation_id
    marker.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def seat_worker(workspace: str | None, worker_conversation_id: int) -> dict:
    """Assign a desk to a worker chat: claim a seat, then stamp the marker
    so the seat is occupied until the worker chat is deleted."""
    seat = await claim_desk(workspace)
    await _write_marker(Path(seat["dir"]), worker_conversation_id)
    return seat


async def release_desk(workspace: str | None, worker_conversation_id: int) -> dict | None:
    """Unseat a worker chat. The desk directory itself stays — it outlives
    its worker chat and the next occupant inherits it."""
    pool = pool_dir(workspace)
    if not pool.is_dir():
        return None
    for seat_dir in pool.iterdir():
        if not (seat_dir.is_dir() and seat_dir.name.isdigit()):
            continue
        marker = seat_dir / DESK_MARKER
        if not marker.is_file():
            continue
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if data.get("worker_conversation_id") == worker_conversation_id:
            await _write_marker(seat_dir, None)
            return {"seat": int(seat_dir.name), "dir": str(seat_dir)}
    return None


def pool_cap() -> int:
    from backend.agent.config import load_config

    dw = load_config().get("developer_world") or {}
    try:
        return max(1, int(dw.get("pool_cap", DEFAULT_POOL_CAP)))
    except (TypeError, ValueError):
        return DEFAULT_POOL_CAP


def clean_desk_report(workspace: str | None, worker_conversation_id: int) -> dict:
    """Submit preconditions for the worker's desk. Submit requires a clean
    desk (nothing uncommitted) and at most one pending contribution."""
    from backend.agent.tools import is_git_workspace

    seat = find_seat(workspace, worker_conversation_id)
    if seat is None:
        return {"ok": False, "error": "no desk assigned to this worker chat"}
    seat_dir = Path(seat["dir"])
    if pending_contribution(workspace, worker_conversation_id) is not None:
        return {
            "ok": False,
            "error": (
                "this worker already has a pending contribution — wait for "
                "the supervisor's review (or abandon it) before submitting again"
            ),
        }
    if not is_git_workspace(seat_dir):
        return {"ok": False, "error": "the desk clone is not a git repo"}
    status = _run_git_now(["status", "--porcelain"], seat_dir)
    if status.strip():
        return {
            "ok": False,
            "error": (
                "the desk is dirty — commit (or clean) every change before "
                "submitting. Submit travels a branch, not a worktree state."
            ),
            "status": status[:2000],
        }
    branch = _run_git_now(["branch", "--show-current"], seat_dir)
    return {"ok": True, "dir": str(seat_dir), "branch": branch}


def _run_git_now(args: list[str], cwd: Path) -> str:
    import subprocess

    exe = git_exe() or "git"
    out = subprocess.run(
        [exe, "--no-optional-locks", *args], cwd=str(cwd),
        capture_output=True, text=True, timeout=20,
    )
    return (out.stdout or "").strip()


def find_seat(workspace: str | None, worker_conversation_id: int) -> dict | None:
    pool = pool_dir(workspace)
    if not pool.is_dir():
        return None
    for seat_dir in sorted(pool.iterdir()):
        if not (seat_dir.is_dir() and seat_dir.name.isdigit()):
            continue
        marker = seat_dir / DESK_MARKER
        if not marker.is_file():
            continue
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if data.get("worker_conversation_id") == worker_conversation_id:
            return {"seat": int(seat_dir.name), "dir": str(seat_dir)}
    return None


def pool_status(workspace: str | None) -> dict:
    pool = pool_dir(workspace)
    seats = []
    if pool.is_dir():
        for seat_dir in sorted(pool.iterdir()):
            if not (seat_dir.is_dir() and seat_dir.name.isdigit()):
                continue
            occupant = None
            marker = seat_dir / DESK_MARKER
            if marker.is_file():
                try:
                    occupant = json.loads(marker.read_text(encoding="utf-8")).get(
                        "worker_conversation_id"
                    )
                except Exception:  # noqa: BLE001
                    occupant = None
            seats.append({"seat": int(seat_dir.name), "worker_conversation_id": occupant})
    return {"pool": str(pool), "cap": pool_cap(), "seats": seats}


def desk_root_for_worker(workspace: str | None, worker_conversation_id: int) -> str | None:
    """The desk dir for a seated worker, or None when unseated."""
    seat = find_seat(workspace, worker_conversation_id)
    return seat["dir"] if seat else None




# ---- tool executors (registered in backend/agent/tools.py) ------------------


async def spawn_worker_tool(
    workspace: str,
    task: str = "",
    name: str = "",
    conversation_id: int | None = None,
) -> dict:
    """spawn_worker executor (supervisor side, ordinary chats only).

    Creates a chat_type='worker' conversation parented by the calling
    chat, seats it at a desk from the workspace's pool, and fires `task`
    as its first turn via the shared wake primitive."""
    from backend.db.database import create_conversation, get_conversation
    from backend.main import wake_conversation

    if conversation_id is None:
        # UI-spawned worker: no parent chat — the human supervises through
        # the Workers panel and its submits sit in the inbox un-woken.
        parent_id = None
    else:
        parent_id = int(conversation_id)
        parent = await get_conversation(parent_id)
        if parent is None:
            return {"error": f"no conversation {parent_id}"}
        if (parent.get("chat_type") or "chat") != "chat":
            return {
                "error": "only an ordinary chat can spawn a worker — workers "
                "cannot spawn workers"
            }
    conv_ws = (
        (parent.get("workspace") if parent_id is not None else workspace) or ""
    ).strip()
    if not conv_ws or conv_ws.startswith("remote:"):
        return {
            "error": "worker chats need a local git workspace — desks clone "
            "the workspace repo, and remote workspaces are out of scope (v1)"
        }
    from backend.agent.tools import is_git_workspace, workspace_root

    if not is_git_workspace(workspace_root(conv_ws)):
        return {
            "error": "this workspace has no git checkout — worker chats are "
            "unavailable without one"
        }
    try:
        seat = await seat_worker(conv_ws, 0)  # claimed before the row exists
    except RuntimeError as e:
        return {"error": str(e)}
    worker_id = await create_conversation(
        title=(name or "Worker").strip()[:80] or "Worker",
        workspace=conv_ws,
        chat_type="worker",
        worker_parent_id=parent_id,
        desk_dir=seat["dir"],
    )
    await _write_marker(Path(seat["dir"]), worker_id)
    fired = None
    if task and task.strip():
        from backend.main import _worker_first_prompt

        fired = await wake_conversation(
            worker_id, _worker_first_prompt(task.strip()), source="spawn"
        )
    return {
        "worker_conversation_id": worker_id,
        "desk": seat["dir"],
        "seat": seat["seat"],
        "desk_branch": seat["branch"],
        "reused_desk": seat["reused"],
        "first_turn": fired,
        "task": (task or "").strip(),
    }


async def submit_contribution_tool(
    workspace: str,
    summary: str = "",
    branch: str = "",
    base_commit: str = "",
    title: str = "",
    conversation_id: int | None = None,
) -> dict:
    """submit_contribution executor (worker side only): file the desk's
    branch into the workspace inbox and wake the supervising chat."""
    from backend.db.database import get_conversation
    from backend.agent.gitexec import run_git

    if not summary or not summary.strip():
        return {"error": "summary is required: say what the contribution does"}
    if not branch or not branch.strip():
        return {"error": "branch is required: commit your work on a branch first"}
    if conversation_id is None:
        return {"error": "submit_contribution runs only from a worker chat"}
    worker_id = int(conversation_id)
    conv = await get_conversation(worker_id)
    if conv is None or (conv.get("chat_type") or "chat") != "worker":
        return {"error": "submit_contribution runs only in a worker chat"}
    conv_ws = (conv.get("workspace") or "").strip()
    if not conv_ws or conv_ws.startswith("remote:"):
        return {"error": "worker chats on remote or empty workspaces cannot submit"}
    desk_dir = conv.get("desk_dir") or None
    if not desk_dir:
        return {"error": "this worker chat has no desk assigned"}
    # Preconditions: clean desk, at most one pending contribution.
    report = clean_desk_report(conv_ws, worker_id)
    if not report.get("ok"):
        return {"error": report.get("error", "desk not ready for submit")}
    rc, out = await run_git(desk_dir, "rev-parse", "HEAD")
    if rc != 0:
        return {"error": f"cannot resolve the desk HEAD: {out.strip()[:200]}"}
    head = out.strip()
    if not (base_commit or "").strip():
        rc, out = await run_git(desk_dir, "merge-base", "workspace/HEAD", head)
        base_commit = out.strip() if rc == 0 and out.strip() else head
    row = add_contribution(
        conv_ws,
        worker_id,
        branch=branch.strip(),
        base_commit=base_commit.strip(),
        summary=summary.strip(),
        desk_dir=str(desk_dir),
        title=(title or "").strip(),
    )
    parent_id = conv.get("worker_parent_id")
    wake = None
    if parent_id:
        from backend.main import wake_conversation

        wake = await wake_conversation(
            int(parent_id),
            "[developer world] A worker contribution is pending in the inbox "
            f"(#{row['id']}, branch {row['branch']}, desk {desk_dir}). Review "
            "and merge it with review_contribution: fetch the branch from the "
            f"desk, inspect, merge into the checkout. Worker chat id: "
            f"{worker_id}. Summary: {row['summary'][:500]}",
            source="contribution",
        )
    return {
        "submitted": True,
        "contribution_id": row["id"],
        "branch": row["branch"],
        "head": head,
        "base_commit": row["base_commit"],
        "wake": wake,
    }


async def message_worker_tool(
    workspace: str,
    worker_conversation_id: int = 0,
    text: str = "",
    conversation_id: int | None = None,
) -> dict:
    """message_worker executor (supervisor side): steer or reply to a
    worker chat. The message wakes the worker — queued behind any active
    turn, started when idle. The funnel injects `conversation_id` (the
    CALLING chat) like CONTEXT_TOOLS (#303); the model supplies
    `worker_conversation_id` (the target worker)."""
    from backend.db.database import get_conversation
    from backend.main import wake_conversation

    if not text or not text.strip():
        return {"error": "text is required"}
    try:
        target = int(worker_conversation_id)
    except (TypeError, ValueError):
        return {"error": "worker_conversation_id must be the worker chat's numeric id"}
    conv = await get_conversation(target)
    if conv is None:
        return {"error": f"no conversation {target}"}
    if (conv.get("chat_type") or "chat") != "worker":
        return {"error": f"conversation {target} is not a worker chat"}
    if (conv.get("workspace") or "").startswith("remote:"):
        # v1 scope: remote workspaces have no desks, so a worker filed
        # under one is out of the model's reach (hand-crafted row at most).
        return {"error": "remote workers cannot be messaged"}
    worker_parent = int(conv.get("worker_parent_id") or 0)
    if (
        worker_parent
        and conversation_id is not None
        and worker_parent != int(conversation_id)
    ):
        return {
            "error": f"worker {target} belongs to chat {worker_parent}, "
            "not to the calling chat"
        }
    result = await wake_conversation(
        target, f"[supervisor] {text.strip()}", source="supervisor"
    )
    return {"sent": result != "gone", "delivery": result}


async def review_contribution_tool(
    workspace: str,
    contribution_id: int = 0,
    decision: str = "",
    feedback: str = "",
    conversation_id: int | None = None,
) -> dict:
    """review_contribution executor (supervisor side): merge, reject, or
    abandon one inbox row. Merge fetches the branch from the desk into the
    checkout; reject/abandon bounce with feedback and free the worker's
    pending slot."""
    from backend.db.database import get_conversation
    from backend.agent.gitexec import run_git

    # The supervisor path is ordinary-chat-only (ADR-0022): a worker never
    # merges — not even its own contribution. Identity rides the funnel-
    # injected conversation id, never model args.
    if conversation_id is not None:
        caller = await get_conversation(int(conversation_id))
        if caller is not None and (caller.get("chat_type") or "chat") == "worker":
            return {
                "error": "workers cannot review contributions — submit your "
                "work and wait for the supervising chat's review"
            }
    conv_ws = (workspace or "").strip()
    if not conv_ws or conv_ws.startswith("remote:"):
        return {"error": "review_contribution runs on a local workspace checkout"}
    row = None
    for candidate in list_inbox(conv_ws, include_closed=True):
        if candidate.get("id") == int(contribution_id):
            row = candidate
            break
    if row is None:
        return {"error": f"no contribution {contribution_id} in this workspace's inbox"}
    if row.get("status") != "pending":
        return {
            "error": f"contribution {contribution_id} is already "
            f"'{row.get('status')}'"
        }
    decision = (decision or "").strip().lower()
    worker_id = int(row.get("worker_conversation_id") or 0)
    desk_dir = row.get("desk_dir") or ""
    branch = row.get("branch") or ""

    if decision == "merge":
        # A dirty checkout defers: user WIP is never integrated through.
        rc, status = await run_git(conv_ws, "status", "--porcelain")
        if rc == 0 and status.strip():
            return {
                "deferred": True,
                "error": (
                    "the workspace checkout is dirty — commit or stash the "
                    "user's WIP first, then call review_contribution again. "
                    "A dirty checkout is never merged through."
                ),
            }
        # Fetch with an explicit refspec: a one-shot path fetch does NOT
        # create remote-tracking refs, so the merge needs the named ref.
        rc, out = await run_git(
            conv_ws, "fetch", str(desk_dir),
            f"+refs/heads/{branch}:refs/remotes/workspace/{branch}",
        )
        if rc != 0:
            return {"error": f"fetch from desk failed: {out.strip()[:400]}"}
        # Merge the fetched branch ref, not FETCH_HEAD: the merge must be
        # the named branch's tip (a second desk branch fetched later would
        # otherwise clobber FETCH_HEAD before this merge runs).
        rc, out = await run_git(
            conv_ws, "merge", "--no-ff", "--no-edit",
            f"workspace/{branch}",
        )
        if rc != 0:
            return {
                "conflict": True,
                "error": (
                    "merge conflict — conflict resolution is yours: resolve "
                    "in the checkout, commit, then re-check the inbox row"
                ),
                "git_output": out.strip()[:600],
            }
        rc, out = await run_git(conv_ws, "rev-parse", "HEAD")
        update_contribution(
            conv_ws, int(contribution_id),
            status="merged", merged_commit=(out.strip() if rc == 0 else ""),
        )
        if worker_id:
            from backend.main import wake_conversation

            await wake_conversation(
                worker_id,
                "[supervisor] Your contribution was merged into the workspace "
                "checkout. Start new work — fetch in the desk and branch from "
                "the updated state.",
                source="supervisor",
            )
        return {
            "merged": True,
            "branch": branch,
            "merged_commit": out.strip() if rc == 0 else "",
        }
    if decision in ("reject", "abandon"):
        update_contribution(
            conv_ws, int(contribution_id),
            status="rejected" if decision == "reject" else "abandoned",
            review=(feedback or "").strip(),
        )
        if worker_id and decision == "reject":
            from backend.main import wake_conversation

            await wake_conversation(
                worker_id,
                f"[supervisor] Your contribution #{row.get('id')} "
                f"({branch}) was REJECTED. Feedback: "
                f"{(feedback or '(none given)').strip()}. Fix the work on "
                "the branch and submit again, or move on.",
                source="supervisor",
            )
        return {
            "decision": decision,
            "contribution_id": int(contribution_id),
            "bounced_to_worker": bool(worker_id and decision == "reject"),
        }
    return {
        "error": "decision must be one of: merge, reject, abandon "
        "(a rejection bounces to the authoring worker with your feedback)"
    }
