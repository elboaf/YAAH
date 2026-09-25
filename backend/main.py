"""AI Coding Agent — FastAPI backend entry point.

Runs as an embedded subprocess inside the Tauri desktop app (or standalone
during development). All model API calls, tool execution, and persistence
flow through this server.
"""
from contextlib import asynccontextmanager

import httpx
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from backend._version import __version__
from backend.db.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Skills are scanned once at startup; the UI can force a rescan via
    # POST /api/skills/refresh. The directory is created on first run so
    # there is an obvious place to drop skills.
    from backend.agent import skills as skills_registry

    skills_registry.ensure_dir()
    skills_registry.ensure_scanned()
    # LAN hosting (on by default): advertise this backend over mDNS so
    # other YAAH instances can discover it. A host that can't advertise
    # is still reachable by direct IP; failures are logged, never fatal.
    # YAAH_NO_HOSTING (the headless server's --no-hosting) skips the
    # beacon for that run without touching the stored setting.
    import os as _os

    from backend.agent import discovery
    from backend.agent.config import load_config

    if _os.environ.get("YAAH_NO_HOSTING") != "1" and (
        load_config().get("remote") or {}
    ).get("hosting_enabled", True):
        discovery.start_advertising(API_PORT)
    # MCP tool servers (config "mcpServers"): spawn each registered server
    # and merge its tools into the model's toolbox. Failures are per-server
    # and non-fatal — a broken server shows as 'failed' in Settings.
    from backend.agent import mcp_client

    mcp_client.manager.start_all()
    # Computer use (Windows only): real-input activity detector + panic
    # hotkey listener. Never fatal — a failure just means no pause/no hotkey.
    # Skipped entirely in headless mode (the yaah-server binary): there is
    # no interactive session to hook on a headless box.
    if os.name == "nt" and os.environ.get("YAAH_HEADLESS") != "1":
        from backend.agent import computer

        computer.start_background()
    # Scheduled agents (issue #41): overdue next_fire slots roll forward
    # (missed fires skip silently — no catch-up), then the due-run tick
    # starts if any agent exists. Fires only while a backend process is
    # alive: desktop open, or the headless yaah-server service.
    from backend.agent import scheduler

    await scheduler.ensure_scheduled()
    # Issue #58: the worktree reaper (salvage-before-delete for worktrees
    # orphaned by crashed/aborted runs, then TTL pruning).
    from backend.agent import worktrees as _worktrees

    _worktrees.start_reaper()
    # Issue #98 / adr/0002: keep every visible main tree fast-forwarded to
    # its upstream on a background cadence (ff-only, overlap-aware) so a
    # refused or crashed merge-back can never leave the user's folder
    # silently behind the work that shipped.
    _worktrees.start_background_sync()
    yield
    await mcp_client.manager.shutdown()
    scheduler.stop_scheduler()
    _worktrees.stop_background_sync()
    _worktrees.stop_reaper()
    discovery.stop_advertising()


# The sidecar always serves on this port (backend_entry.py, lib.rs spawn).
API_PORT = 8765

app = FastAPI(title="AI Coding Agent", version=__version__, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # The Tauri webview origin differs per platform: tauri://localhost on
    # macOS, http(s)://tauri.localhost on Windows/Linux. Cover vite dev and
    # any tauri origin; the server only ever listens on localhost.
    allow_origin_regex=r"^https?://(localhost|tauri\.localhost)(:\d+)?$|^tauri://localhost$",
    allow_methods=["*"],
    allow_headers=["*"],
)


def _is_loopback_client(client_host: str) -> bool:
    """True for the local UI's connections. Non-IP clients (the ASGI test
    transport's 'testclient') count as local tooling, not remote."""
    import ipaddress

    if not client_host:
        return True
    try:
        return ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        return True


@app.middleware("http")
async def remote_auth_guard(request, call_next):
    """Network gate: localhost stays trust-based (the local UI never sends
    secrets or headers), but every request from beyond loopback — and every
    loopback request carrying the X-Yaah-Remote marker (the client proxy) —
    must present the host's passphrase. A host with no passphrase set
    refuses all remote access, so binding 0.0.0.0 with hosting on exposes
    nothing unauthenticated."""
    import ipaddress

    from fastapi.responses import JSONResponse

    from backend.agent.config import load_config

    client_host = (request.client.host if request.client else "") or ""
    try:
        is_local = not client_host or ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_local = True  # non-IP client (ASGI test transport) = local tooling
    marked = bool(request.headers.get("x-yaah-remote"))
    # The handshake and health probes stay open from anywhere: they carry no
    # secrets and are how a client learns the protocol/instance id before it
    # can know the passphrase.
    path_open = request.url.path in ("/api/health", "/api/remote/info")
    if (marked or not is_local) and not path_open:
        # YAAH_PASSPHRASE (set via systemd's EnvironmentFile=/etc/yaah/yaah.conf
        # on the Linux daemon) takes precedence over ~/.yaah/config.json.
        expected = (
            os.environ.get("YAAH_PASSPHRASE")
            or (load_config().get("remote") or {}).get("passphrase")
            or ""
        )
        got = request.headers.get("x-yaah-passphrase") or ""
        if not expected or got != expected:
            detail = (
                "remote access refused: no passphrase set on this host (set one under Settings)"
                if not expected
                else "remote access refused: wrong passphrase"
            )
            return JSONResponse({"detail": detail}, status_code=401)
    return await call_next(request)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ---- Conversation / Message API ----

from pydantic import BaseModel

from backend.db.database import (
    add_message,
    create_conversation,
    delete_workspace,
    get_conversation,
    get_messages,
    get_workspace_by_path,
    list_conversations,
    list_workspaces,
    move_conversation,
    touch_workspace,
    update_conversation,
    delete_conversation,
    upsert_workspace,
    upsert_remote_conversation,
    list_remote_conversations,
    get_remote_messages,
)


class NewConversation(BaseModel):
    title: str = "New Task"
    workspace: str | None = None
    # Per-chat scope (#51/#76): the chat's pinned model ("provider::model" or
    # bare id) and effort ('' = param not sent). Blank strings are legal —
    # they are the chat's deliberate Default, distinct from "unspecified".
    model: str = ""
    effort: str = ""


class ConversationUpdate(BaseModel):
    title: str | None = None
    workspace: str | None = None
    system_prompt_override: str | None = None
    # #51/#76: the header pickers write these; '' = Default (param not sent).
    model: str | None = None
    effort: str | None = None


class ConversationMove(BaseModel):
    # Destination workspace path; null = the Default pseudo-workspace
    # (no root directory). The turn endpoint derives the working directory
    # from the conversation row, so THIS is what makes the move real —
    # the sidebar grouping alone would only relabel it (issue #8).
    workspace: str | None = None


class NewMessage(BaseModel):
    role: str
    content: str
    tool_calls: list | None = None
    tool_call_id: str | None = None


@app.post("/api/conversations")
async def api_create_conversation(body: NewConversation):
    cid = await create_conversation(
        body.title, body.workspace, model=body.model, effort=body.effort
    )
    return {"id": cid}


# ---- Workspace registry ----


class NewWorkspace(BaseModel):
    path: str
    owner_id: str | None = None


@app.get("/api/workspaces")
async def api_list_workspaces():
    """Aggregate local workspaces and each saved device's reachable or cached workspaces."""
    local_rows = [
        {**row, "owner_id": None, "device_status": "local"}
        for row in await list_workspaces()
        if remote_mod.parse_ns(row["path"]) is None
    ]
    profiles = _remote_device_profiles()
    if not profiles and remote_mod.get_remote() is not None:
        host = remote_mod.get_remote()
        try:
            rows = _proxy_result(await host.proxy("GET", "/api/workspaces/local"))
            remote_rows = [
                {**row, "path": remote_mod.ns_path(host.host_id, row.get("path")),
                 "owner_id": host.host_id, "device_status": "online"}
                for row in rows
            ]
            return local_rows + remote_rows
        except (httpx.HTTPError, HTTPException):
            return local_rows

    result = list(local_rows)
    for profile in profiles:
        host_id = profile["host_id"]
        host = remote_mod.get_remote(host_id)
        rows = profile.get("cached_workspaces") or []
        state = "offline"
        if host is not None:
            try:
                rows = _proxy_result(await host.proxy("GET", "/api/workspaces/local"))
                rows = [
                    {**row, "path": remote_mod.ns_path(host_id, row.get("path")),
                     "owner_id": host_id, "device_status": "online"}
                    for row in rows
                ]
                profile["cached_workspaces"] = rows
                profile.update({"name": host.name, "os": host.info.get("os")})
                state = "online"
            except (httpx.HTTPError, HTTPException):
                rows = [
                    {**row, "owner_id": host_id, "device_status": "error"}
                    for row in rows
                ]
                state = "error"
        else:
            rows = [
                {**row, "owner_id": host_id, "device_status": "cached"}
                for row in rows
            ]
        result.extend(rows)
        profile["status"] = state
    _save_remote_device_profiles(profiles)
    return result

@app.get("/api/workspaces/local")
async def api_list_local_workspaces():
    """This machine's registry only — the sidebar greys these out while a
    remote host is connected."""
    return [
        r for r in await list_workspaces()
        if remote_mod.parse_ns(r["path"]) is None
    ]


@app.post("/api/workspaces")
async def api_add_workspace(body: NewWorkspace):
    """Register a folder on its explicit owner, or locally for local paths."""
    import os

    host = _workspace_host(body.path, body.owner_id)
    if host is None:
        # Typed paths need normalizing before anything resolves them: `~` does
        # not expand itself, and a bare name must anchor to the home directory
        # (the Default workspace), not the backend's root-owned install cwd.
        # NOT done for host-bound paths: expanduser/isabs would apply THIS
        # machine's OS rules to a path that must resolve on the HOST's — a
        # Linux-style path typed on a Windows client would be rewritten to
        # C:\\Users\\... before it ever left the machine.
        body.path = os.path.expanduser(body.path.strip())
        if body.path and not os.path.isabs(body.path):
            body.path = str(workspace_root("") / body.path)

    if host is not None:
        raw = remote_mod.parse_ns(body.path)
        remote_path = raw[1] if raw else body.path.strip()
        res = await host.proxy(
            "POST", "/api/workspaces", json_body={"path": remote_path}
        )
        row = _proxy_result(res)
        row["path"] = remote_mod.ns_path(host.host_id, row.get("path"))
        row["owner_id"] = host.host_id
        row["device_status"] = "online"
        return row
    ws = await upsert_workspace(body.path)
    ws["exists"] = True if ws["path"] is None else os.path.isdir(ws["path"])
    if ws["path"] is not None and not ws["exists"]:
        # Registering a folder that doesn't exist yet (the remote "add folder
        # on host" flow has no native picker, so paths are typed): create it
        # instead of leaving a permanent "missing" ghost entry.
        try:
            os.makedirs(ws["path"], exist_ok=True)
            ws["exists"] = True
        except OSError:
            pass
    return ws


@app.delete("/api/workspaces/{workspace_id}")
async def api_delete_workspace(workspace_id: int, owner_id: str | None = None):
    """Remove a workspace; its conversations relocate to Default (host-side
    registry and host-side relocation while connected)."""
    from fastapi import HTTPException

    host = _workspace_host("", owner_id)
    if host is not None:
        res = await host.proxy("DELETE", f"/api/workspaces/{workspace_id}")
        return _proxy_result(res)

    try:
        result = await delete_workspace(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="workspace not found")
    return {"ok": True, **result}


@app.get("/api/conversations")
async def api_list_conversations():
    # Agent chats are a chat_type on the row itself (issue #41); the UI
    # pins them under their workspace from this field.
    return await list_conversations()


@app.get("/api/conversations/{conversation_id}")
async def api_get_conversation(conversation_id: int):
    conv = await get_conversation(conversation_id)
    if conv is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


@app.get("/api/conversations/{conversation_id}/context")
async def api_conversation_context(conversation_id: int):
    """Exact context size of the session's latest model call (usage
    prompt_tokens persisted by the agent loop) plus the context window to
    display it against. Window resolution: Settings override for this model
    -> provider-reported value -> built-in table -> null (UI shows tokens
    without the %/bar)."""
    conv = await get_conversation(conversation_id)
    if conv is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="conversation not found")
    from backend.agent.context_window import get_context_window

    cfg = load_config()
    # The chip resolves against the model THIS chat actually uses (#51) —
    # the conversation's pinned model, falling back to the global default
    # (pre-upgrade rows). "provider::model" pins strip to the bare id: the
    # window depends on the model, not the provider routing.
    model = conv.get("model") or cfg.get("model") or None
    if model and "::" in model:
        model = model.partition("::")[2] or model
    return {
        "context_tokens": conv.get("context_tokens"),
        "context_model": conv.get("context_model"),
        "model": model,
        "context_window": await get_context_window(model, cfg),
    }


@app.get("/api/workspaces/git-branches")
async def api_workspace_git_branches(workspace: str = ""):
    """Current branch and local branch names for an unsaved draft workspace.

    Draft chats have no conversation id yet, so keep this read path keyed by
    the destination path instead. Default and remote destinations have no
    local checkout to inspect.
    """
    workspace = workspace.strip()
    if not workspace or workspace.startswith("remote:"):
        return {"branch": None, "branches": []}

    from backend.agent.gitinfo import current_git_branch, list_local_branches
    from backend.agent.tools import workspace_root
    from backend.agent import worktrees

    try:
        root = workspace_root(workspace)
    except ValueError:
        return {"branch": None, "branches": []}
    branches = await list_local_branches(root)
    if not branches:
        return {"branch": None, "branches": []}
    return {
        "branch": await current_git_branch(root),
        "branches": worktrees.filter_agent_branches(branches),
    }


class WorkspaceGitCheckoutBody(BaseModel):
    workspace: str
    branch: str


@app.post("/api/workspaces/git-checkout")
async def api_workspace_git_checkout(body: WorkspaceGitCheckoutBody):
    """Checkout a selected local branch before a draft conversation exists.

    Like the saved-chat checkout, this is user-initiated, shell-free, and
    serialized with agent merges. Git's own dirty-worktree refusal is returned
    verbatim to the card rather than hidden.
    """
    from fastapi import HTTPException
    from backend.agent.gitinfo import invalidate_git_caches, list_local_branches
    from backend.agent.tools import workspace_root
    from backend.agent import worktrees

    workspace = body.workspace.strip()
    branch = body.branch.strip()
    if not workspace or workspace.startswith("remote:"):
        return {"ok": False, "error": "no local git workspace"}
    if not branch:
        return {"ok": False, "error": "checkout target is empty"}
    if branch.startswith("-"):
        return {"ok": False, "error": "checkout target must be a local branch name"}
    try:
        root = workspace_root(workspace)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        async with worktrees.merge_mutex(root):
            branches = await list_local_branches(root)
            if branch not in branches:
                return {"ok": False, "error": f"not a local branch: {branch}"}
            result = await _run_ui_git(root, "checkout", branch)
            invalidate_git_caches(root)
            return {"ok": "error" not in result, **result}
    except TimeoutError:
        return {
            "ok": False,
            "error": "another agent merge or git operation is in progress; try again",
        }


@app.get("/api/conversations/{conversation_id}/git-branch")
async def api_conversation_git_branch(conversation_id: int):
    """Current branch of the conversation's workspace, when it is a git repo.

    Cheap by design: stats .git/HEAD first and only spawns git when the file
    changed — the UI re-polls this every couple of seconds while a session
    is open, and terminal checkouts reflect without any push channel."""
    conv = await get_conversation(conversation_id)
    if conv is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="conversation not found")
    from backend.agent.gitinfo import current_git_branch
    from backend.agent.tools import workspace_root

    ws = conv.get("workspace") or ""
    if not ws.strip() or ws.startswith("remote:"):
        return {"branch": None}
    try:
        root = workspace_root(ws)
    except ValueError:
        return {"branch": None}
    return {"branch": await current_git_branch(root)}


@app.get("/api/conversations/{conversation_id}/git-info")
async def api_conversation_git_info(conversation_id: int):
    """Full git readout for the status strip: branch, dirty state, +N −N
    line counts, local vs upstream hashes and ahead/behind. TTL-cached in
    gitinfo (one git burst per ~2s per workspace) — the UI polls this while
    a session is open."""
    conv = await get_conversation(conversation_id)
    if conv is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="conversation not found")
    from backend.agent.gitinfo import git_workspace_info
    from backend.agent.tools import workspace_root

    ws = conv.get("workspace") or ""
    if not ws.strip() or ws.startswith("remote:"):
        return {"info": None}
    try:
        root = workspace_root(ws)
    except ValueError:
        return {"info": None}
    return {"info": await git_workspace_info(root)}


@app.get("/api/conversations/{conversation_id}/git-branches")
async def api_conversation_git_branches(conversation_id: int):
    """Local branch names for the chip's checkout dropdown (not a repo -> [])."""
    conv = await get_conversation(conversation_id)
    if conv is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="conversation not found")
    from backend.agent.gitinfo import list_local_branches
    from backend.agent.tools import workspace_root

    from backend.agent import worktrees

    ws = conv.get("workspace") or ""
    if not ws.strip() or ws.startswith("remote:"):
        return {"branches": []}
    try:
        root = workspace_root(ws)
    except ValueError:
        return {"branches": []}
    branches = await list_local_branches(root)
    # Issue #58 hygiene: `agent/*` merge-back branches are harness
    # artifacts, not checkout targets — never offer them to the user.
    return {"branches": worktrees.filter_agent_branches(branches)}


class GitCommandBody(BaseModel):
    action: str  # status | commit | push | pull | checkout
    message: str | None = None  # commit message
    branch: str | None = None  # checkout target


_GIT_ACTIONS = {"status", "commit", "push", "pull", "checkout"}


async def _conversation_git_root(conversation_id: int):
    """Resolved workspace root for a conversation, or None when the session
    has no local git workspace (remote:/empty/non-repo handled by callers)."""
    from backend.agent.tools import workspace_root

    conv = await get_conversation(conversation_id)
    if conv is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="conversation not found")
    ws = conv.get("workspace") or ""
    if not ws.strip() or ws.startswith("remote:"):
        return None
    try:
        return workspace_root(ws)
    except ValueError:
        return None


async def _run_ui_git(root, *args: str) -> dict:
    """One UI-initiated git command, shell-free (argument list exec — a
    crafted branch name or commit message cannot become a second command)."""
    import asyncio

    from backend.agent.tools import _NO_WINDOW, _NEW_SESSION

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_NO_WINDOW,
            **_NEW_SESSION,
        )
    except OSError as e:
        return {"error": f"git not runnable: {e}"}
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": "git timed out"}
    text = out.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return {"error": text[:2000] or f"git exited {proc.returncode}"}
    return {"output": text[:2000]}


async def _post_git_trace(conversation_id: int, action: str, result: dict) -> None:
    """Record the command in the conversation as a synthetic tool row: it
    renders through the normal tool-trace UI, survives reloads, and is never
    replayed into model context (load_history drops rows whose tool_call_id
    has no matching assistant call)."""
    import json as _json

    call_id = f"ui-git-{action}-{os.urandom(4).hex()}"
    await add_message(
        conversation_id,
        "tool",
        _json.dumps(result),
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": f"git {action}",
                    "arguments": _json.dumps({}),
                },
            }
        ],
        tool_call_id=call_id,
    )


@app.post("/api/conversations/{conversation_id}/git-command")
async def api_conversation_git_command(conversation_id: int, body: GitCommandBody):
    """Human-initiated git action from the status strip. Deliberately NOT
    gated by access mode — that gate throttles the agent, not the user at
    their own machine. Whitelist-only actions; everything lands in the
    conversation as a trace row."""
    from backend.agent.gitinfo import invalidate_git_caches

    from backend.agent import worktrees

    action = body.action
    if action not in _GIT_ACTIONS:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=f"unsupported git action: {action}")

    root = await _conversation_git_root(conversation_id)
    if root is None:
        return {"ok": False, "error": "no local git workspace"}

    # Issue #58 (review decision 3): UI git operations are writers too —
    # they run under the same merge mutex so a checkout/pull/commit can
    # never interleave with an agent merge-back. Mutating actions only;
    # `status` stays lock-free.
    if action != "status":
        try:
            async with worktrees.merge_mutex(root):
                return await _ui_git_locked(
                    root, conversation_id, action, body, invalidate_git_caches
                )
        except TimeoutError:
            return {
                "ok": False,
                "error": "another agent merge or git operation is in progress; try again",
            }
    return await _ui_git_locked(
        root, conversation_id, action, body, invalidate_git_caches
    )


async def _ui_git_locked(root, conversation_id: int, action: str, body: GitCommandBody, invalidate) -> dict:
    """The git-command body, run while holding the merge mutex (or for
    read-only `status`). Ends with the standard invalidate+trace+return."""
    if action == "status":
        result = await _run_ui_git(root, "status", "--short", "--branch")
    elif action == "commit":
        msg = (body.message or "").strip()
        if not msg:
            return {"ok": False, "error": "commit message is empty"}
        added = await _run_ui_git(root, "add", "-A")
        if "error" in added:
            result = added
        else:
            result = await _run_ui_git(root, "commit", "-m", msg)
    elif action == "push":
        result = await _run_ui_git(root, "push")
        err = result.get("error", "").lower()
        if "error" in result and ("upstream" in err or "push destination" in err):
            # No upstream (new branch) or no remote configured yet: retry
            # with --set-upstream and say so in the recorded output.
            probe = await _run_ui_git(root, "rev-parse", "--abbrev-ref", "HEAD")
            branch = probe.get("output", "").strip() if "output" in probe else ""
            if branch:
                retried = await _run_ui_git(
                    root, "push", "--set-upstream", "origin", branch
                )
                retried.setdefault("note", f"set upstream to origin/{branch}")
                result = retried
    elif action == "pull":
        result = await _run_ui_git(root, "pull")
    elif action == "checkout":
        branch = (body.branch or "").strip()
        if not branch:
            return {"ok": False, "error": "checkout target is empty"}
        result = await _run_ui_git(root, "checkout", branch)

    invalidate(root)
    await _post_git_trace(conversation_id, action, result)
    return {"ok": "error" not in result, **result}


@app.patch("/api/conversations/{conversation_id}")
async def api_update_conversation(conversation_id: int, body: ConversationUpdate):
    from fastapi import HTTPException

    conv = await get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    # #51/#76: a pinned agent chat must never silently diverge from its
    # agent — the header selectors there write through PATCH /api/agents/{id}
    # instead (the UI routes them; a stale client gets told where to write).
    if (conv.get("chat_type") or "chat") == "agent" and (
        body.model is not None or body.effort is not None
    ):
        raise HTTPException(
            status_code=409,
            detail="this chat is owned by a scheduled agent — change the "
            "agent's model/effort, not the chat's",
        )
    fields = {}
    for name in ("title", "workspace", "system_prompt_override", "model", "effort"):
        val = getattr(body, name)
        if val is not None:
            fields[name] = val
    ok = await update_conversation(conversation_id, **fields)
    return {"ok": ok}


@app.post("/api/conversations/{conversation_id}/move")
async def api_move_conversation(conversation_id: int, body: ConversationMove):
    """Move a chat to another workspace (issue #8).

    Re-files the conversation row under the target — which is also what the
    turn endpoint uses as the working directory for every future turn, so
    the chat's next message RUNS in the target workspace, not just sits
    under its group in the sidebar. Refuses while a run is streaming (the
    in-flight turn's tools already point at the old workspace; a move
    mid-run would strand the merge-back in the wrong tree) or while
    messages sit queued on that run (they were written against the old
    workspace's context)."""
    from fastapi import HTTPException

    from backend.agent.loop import agent_is_running, queue_items

    conv = await get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    if agent_is_running(conversation_id):
        raise HTTPException(
            status_code=409,
            detail="a run is active in this chat — stop it before moving",
        )
    if queue_items(conversation_id):
        raise HTTPException(
            status_code=409,
            detail="this chat has queued messages — send or discard them before moving",
        )
    result = await move_conversation(conversation_id, body.workspace)
    if result is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    # Touch the destination so it re-sorts to the top of the sidebar picker.
    await touch_workspace(result["workspace"])
    return {
        "ok": True,
        "workspace": result["workspace"],
        "target_id": result["target_id"],
    }


@app.delete("/api/conversations/{conversation_id}")
async def api_delete_conversation(conversation_id: int):
    # adr/0003: deleting the chat ends its worktree session — the
    # session-end teardown (trash drop, salvage, branch hygiene) runs
    # here, once, instead of at every turn end. A chat with a live run
    # keeps its session: the turn's own finally still merges, and the
    # reaper collects the orphan after the TTL.
    from backend.agent import worktrees as _wt
    from backend.agent.loop import agent_is_running

    if not agent_is_running(conversation_id):
        await _wt.release_session(str(conversation_id), why="chat deleted")
    ok = await delete_conversation(conversation_id)
    if not ok:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="conversation not found")
    return {"ok": True}


@app.get("/api/conversations/{conversation_id}/messages")
async def api_get_messages(conversation_id: int):
    return await get_messages(conversation_id)


@app.post("/api/conversations/{conversation_id}/messages")
async def api_add_message(conversation_id: int, body: NewMessage):
    mid = await add_message(
        conversation_id, body.role, body.content, body.tool_calls, body.tool_call_id
    )
    return {"id": mid}


# ---- Agent streaming endpoint ----

from fastapi.responses import StreamingResponse

from backend.agent.config import load_config, save_config, set_active_model, set_last_workspace
from backend.agent.loop import run_agent


class AgentTurn(BaseModel):
    message: str
    workspace: str
    images: list[str] = []  # image data URLs attached by the user
    skills: list[str] = []  # skill names invoked via /s or chips
    # True when continuing an interrupted turn: the user message is already
    # stored, so the loop must not persist it again.
    resume: bool = False


def _resolve_turn_scope(
    conv: dict | None, agent: dict | None = None
) -> tuple[str, str | None]:
    """Per-chat model + effort resolution (#51/#76): what a turn in this
    conversation runs on, before any streaming starts.

    - Normal chat: the conversation row pins both — the sidebar default and
      the Settings effort were stamped in at first send and only change when
      the chat's own pickers change them. effort '' (the chat's explicit
      Default) becomes the None sentinel so the reasoning_effort param is
      NOT sent even if the global setting would send it.
    - Agent-pinned chat: resolve through the owning agent — the header
      selectors write through to the agent, so its values ARE the chat's.
      Agent effort '' means inherit-global here, matching the semantics the
      scheduler uses when it fires the same agent (an interactive turn in
      the pinned chat can't diverge from a scheduled one).
    - Unstamped rows (conv.model blank from a pre-upgrade row the stamp
      never reached) resolve to the global defaults: "" model, None effort.

    Returns (model_override, effort_override) for run_agent. Provider-down
    fails the turn visibly in model_client — no fallback is substituted.
    """
    if agent is not None:
        return (
            agent.get("model") or "",
            (agent.get("effort") or "").strip().lower(),
        )
    if conv is not None:
        model = conv.get("model") or ""
        raw_effort = (conv.get("effort") or "").strip().lower()
        return model, (raw_effort or None)
    return "", None


class ProviderEntry(BaseModel):
    api_base: str | None = None
    api_key: str | None = None
    model: str | None = None


class ConfigUpdate(BaseModel):
    providers: dict[str, ProviderEntry] | None = None
    active_provider: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    max_steps: int | None = None
    # Reasoning effort (#6): "" = don't send the param; provider-advertised values send it.
    reasoning_effort: str | None = None
    voice: dict | None = None
    remote: dict | None = None
    ui_scale: float | None = None
    context_window_overrides: dict[str, int | None] | None = None
    access_mode: str | None = None
    compaction: dict | None = None


@app.post("/api/agent/{conversation_id}")
async def api_agent_turn(conversation_id: int, body: AgentTurn):
    """Run one agent turn; stream JSON-line events."""
    # One turn at a time per conversation: reject early so the UI can say so
    # instead of interleaving two streams into one chat. (run_agent re-checks
    # atomically in case of a race.)
    from backend.agent.loop import agent_is_running

    if agent_is_running(conversation_id):
        raise HTTPException(status_code=409, detail="conversation already running")
    # Working directory for this turn (issue #8): the conversation row's
    # workspace — the same column the sidebar groups by, so a moved chat's
    # next message runs inside the workspace it was moved TO, and a stale
    # client (or a hand-rolled request) can never stream the chat against
    # a directory it is no longer filed under.
    conv = await get_conversation(conversation_id)
    turn_workspace = (conv or {}).get("workspace") or ""
    # Per-chat model + effort (#51/#76): resolve what this turn runs on.
    agent = None
    if conv is not None and (conv.get("chat_type") or "chat") == "agent":
        agent = await get_agent_for_conversation(conversation_id)
    turn_model, turn_effort = _resolve_turn_scope(conv, agent)
    set_last_workspace(turn_workspace)
    # Remember it so the sidebar restores the same folder after an app
    # restart, and touch the registry row so the dropdown/group order
    # reflects recent activity.
    set_last_workspace(turn_workspace)
    await touch_workspace(turn_workspace or None)
    # Attached images: decode data URLs to files on disk up front; only the
    # rel paths travel into the agent loop and the database.
    from backend.agent.imagedata import save_data_url

    image_paths = []
    for image in body.images[:4]:  # cap at 4 images per message
        if image.startswith("data:"):
            rel = save_data_url(image, subdir=str(conversation_id))
        else:
            # Queue autosend hands off stored relative paths. load_data_url
            # validates containment and existence before accepting one.
            from backend.agent.imagedata import load_data_url

            rel = image if load_data_url(image) else None
        if rel:
            image_paths.append(rel)
    return StreamingResponse(
        run_agent(conversation_id, body.message, turn_workspace,
                  image_paths=image_paths, skill_names=body.skills,
                  persist_user=not body.resume,
                  model_override=turn_model, effort_override=turn_effort),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/images/{rel:path}")
async def api_image(rel: str):
    """Serve a stored image (chat attachments and view_image downloads)."""
    from fastapi.responses import FileResponse

    from backend.agent.imagedata import IMAGES_ROOT

    path = (IMAGES_ROOT / rel).resolve()
    if IMAGES_ROOT.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    return FileResponse(path)


class NewAttachment(BaseModel):
    workspace: str
    name: str
    content: str


@app.post("/api/attachments")
async def api_save_attachment(body: NewAttachment):
    """Stage a user-attached text file inside the workspace so the agent's
    workspace-sandboxed read_file tool can open it on demand. Proxied to
    the host while a remote session is active (the file lands in the
    host's workspace, where its read_file will look)."""
    import os

    host = _workspace_host(body.workspace)
    if host is not None:
        res = await host.proxy(
            "POST",
            "/api/attachments",
            json_body={**body.model_dump(), "workspace": _host_ws(host, body.workspace)},
        )
        return _proxy_result(res)

    from backend.agent.tools import resolve_path

    if "\u0000" in body.content:
        raise HTTPException(status_code=415, detail="binary file content rejected")
    if len(body.content.encode("utf-8")) > 2_000_000:
        raise HTTPException(status_code=413, detail="attachment exceeds the 2 MB limit")
    name = os.path.basename(body.name.replace("\\", "/")) or "attachment"
    try:
        folder = resolve_path(body.workspace, ".yaah-attachments", for_write=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    folder.mkdir(parents=True, exist_ok=True)
    stem, dot, ext = name.rpartition(".")
    candidate = folder / name
    n = 1
    while candidate.exists():
        candidate = folder / f"{stem or name}-{n}{dot + ext if dot else ''}"
        n += 1
    candidate.write_text(body.content, encoding="utf-8")
    return {"path": f".yaah-attachments/{candidate.name}"}


@app.post("/api/agent/{conversation_id}/cancel")
async def api_agent_cancel(conversation_id: int):
    """Ask a running agent turn to stop after its current step."""
    from backend.agent.loop import cancel_agent

    cancel_agent(conversation_id)
    return {"ok": True}


# ---- Message queue + steering (issue #7) ----

class QueueBody(BaseModel):
    message: str
    skills: list[str] | None = None
    images: list[str] | None = None


@app.post("/api/agent/{conversation_id}/queue")
async def api_agent_queue(conversation_id: int, body: QueueBody):
    """Queue a message while a run is in flight (#7). Held server-side with
    the running loop; drained at the next step boundary (soft injection)."""
    from backend.agent.loop import agent_is_running, enqueue_message

    if not agent_is_running(conversation_id):
        raise HTTPException(status_code=409, detail="conversation is not running")
    text = body.message.strip()
    if not text and not body.images:
        raise HTTPException(status_code=400, detail="message must not be empty")
    from backend.agent.imagedata import save_data_url

    image_paths = []
    for data_url in (body.images or [])[:4]:
        rel = save_data_url(data_url, subdir=str(conversation_id))
        if rel:
            image_paths.append(rel)
    if not text and not image_paths:
        raise HTTPException(status_code=400, detail="message must contain text or a valid image")
    item = enqueue_message(conversation_id, text, body.skills, image_paths)
    return {"ok": True, "item": item}


@app.get("/api/agent/{conversation_id}/queue")
async def api_agent_queue_list(conversation_id: int):
    from backend.agent.loop import queue_items

    return {"items": queue_items(conversation_id)}


@app.delete("/api/agent/{conversation_id}/queue/{item_id}")
async def api_agent_queue_remove(conversation_id: int, item_id: int):
    from backend.agent.loop import remove_queued

    if not remove_queued(conversation_id, item_id):
        raise HTTPException(status_code=404, detail="queued item not found")
    return {"ok": True}


@app.post("/api/agent/{conversation_id}/steer")
async def api_agent_steer(conversation_id: int):
    """Interrupt the in-flight step so queued messages land now (#7).
    The run continues at the next boundary with full context."""
    from backend.agent.loop import agent_is_running, steer_agent

    if not agent_is_running(conversation_id):
        raise HTTPException(status_code=409, detail="conversation is not running")
    if not steer_agent(conversation_id):
        raise HTTPException(status_code=409, detail="nothing to steer")
    return {"ok": True}


class AnswerBody(BaseModel):
    call_id: str
    answer: str


@app.post("/api/conversations/{conversation_id}/answer")
async def api_answer_question(conversation_id: int, body: AnswerBody):
    """Deliver the user's answer to a pending ask_user tool call; the
    blocked agent loop resumes with it as the tool result."""
    from backend.agent.loop import resolve_answer

    if not resolve_answer(conversation_id, body.call_id, body.answer):
        raise HTTPException(
            status_code=409, detail="no pending question for this conversation"
        )
    return {"ok": True}


# ---- Skills ----

from backend.agent import skills as skills_registry


@app.get("/api/skills")
async def api_list_skills():
    """All loaded skills (name, description, manual-only flag, path)."""
    return {"skills": skills_registry.list_skills()}


@app.post("/api/skills/refresh")
async def api_refresh_skills():
    """Rescan the skills directory (Settings / UI refresh)."""
    return {"skills": skills_registry.refresh()}


# ---- MCP tool servers ----

import re as _re

from backend.agent import mcp_client as _mcp
from backend.agent.config import save_config as _save_config


@app.get("/api/mcp/servers")
async def api_mcp_servers():
    """Live status of every configured server + its discovered tools.
    Configured-but-not-yet-running servers show as 'starting' so the
    panel isn't empty right after a save."""
    rows = []
    for name, spec in _mcp.manager.configured().items():
        s = _mcp.manager.servers.get(name)
        if s is None:
            s = _mcp.McpServerState(name, spec if isinstance(spec, dict) else {})
            s.status = "starting"
        rows.append(
            {
                "name": s.name,
                "status": s.status,
                "error": s.error,
                "command": s.spec.get("command", ""),
                "args": s.spec.get("args") or [],
                "tools": [
                    {
                        "name": t["function"]["name"],
                        "description": t["function"]["description"],
                    }
                    for t in s.tools
                ],
            }
        )
    return {"servers": sorted(rows, key=lambda r: r["name"])}


class McpServerBody(BaseModel):
    name: str
    command: str
    args: list[str] = []
    env: dict[str, str] = {}


@app.post("/api/mcp/servers")
async def api_mcp_add_server(body: McpServerBody):
    """Register (or update) a server and (re)connect it. Registration is
    trust: the command runs locally with user permissions."""
    name = body.name.strip()
    if not _re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        raise HTTPException(status_code=400, detail="name: letters/digits/-/_ only")
    if not body.command.strip():
        raise HTTPException(status_code=400, detail="command is required")
    cfg = _mcp.manager.configured()
    cfg[name] = {"command": body.command.strip(), "args": body.args, "env": body.env}
    _save_config({"mcpServers": cfg})
    _mcp.manager.start_all()
    return await api_mcp_servers()


@app.delete("/api/mcp/servers/{name}")
async def api_mcp_remove_server(name: str):
    cfg = _mcp.manager.configured()
    if name not in cfg:
        raise HTTPException(status_code=404, detail=f"no server named {name!r}")
    del cfg[name]
    _save_config({"mcpServers": cfg})
    _mcp.manager.start_all()
    return await api_mcp_servers()


@app.post("/api/mcp/reload")
async def api_mcp_reload():
    """Re-read config and reconcile sessions (restart changed, stop removed)."""
    _mcp.manager.start_all()
    return await api_mcp_servers()


# ---- Scheduled agents (issue #41) ----

from backend.agent import scheduler as scheduler_mod
from backend.db.database import (
    add_instruction,
    create_agent as db_create_agent,
    create_conversation,
    delete_agent as db_delete_agent,
    delete_conversation,
    delete_instruction,
    get_agent as db_get_agent,
    get_agent_for_conversation,
    list_agents as db_list_agents,
    list_instructions,
    update_agent as db_update_agent,
    update_conversation,
    update_instruction,
)


def _new_agent_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def _validate_schedule(schedule_type: str, schedule_spec: dict) -> tuple[str, str]:
    stype, spec = scheduler_mod.normalize_schedule(schedule_type, schedule_spec or {})
    return stype, spec


async def _agent_view(agent: dict) -> dict:
    from backend.agent.loop import agent_is_running

    instructions = await list_instructions(agent["id"])
    conv = None
    if agent.get("conversation_id"):
        conv = await get_conversation(agent["conversation_id"])
    return {
        **agent,
        # SQLite ints -> JSON booleans for the UI.
        "enabled": bool(agent.get("enabled")),
        "memory_enabled": bool(agent.get("memory_enabled")),
        "allow_ask_user": bool(agent.get("allow_ask_user")),
        "notify_on_success": bool(agent.get("notify_on_success")),
        "retention": int(agent.get("retention") or 0),
        "schedule_spec": scheduler_mod.parse_schedule_spec(agent["schedule_spec"]),
        "schedule_text": scheduler_mod.describe_schedule(
            agent["schedule_type"], agent["schedule_spec"]
        ),
        "running": agent_is_running(agent.get("conversation_id") or 0),
        "instructions": instructions,
        "chat_title": (conv or {}).get("title") or agent["name"],
    }


class AgentBody(BaseModel):
    workspace: str = ""          # workspace path; "" = Default (home)
    name: str
    prompt: str
    schedule_type: str = "interval"          # interval | daily | weekly
    schedule_spec: dict = {}                 # see database.SCHEMA agents comment
    approval_policy: str = "sandbox-only"    # sandbox-only | autonomous
    model: str = ""                          # '' = active global model
    effort: str = ""                         # '' | provider-advertised effort
    memory_enabled: bool = True
    # #93: agent-level opt-in — a scheduled run may ask the user a question
    # (ask_user) and wait for the answer in its pinned chat.
    allow_ask_user: bool = False
    retention: int = 0                       # runs kept in the transcript; 0 = unlimited
    notify_on_success: bool = False
    enabled: bool = True


@app.get("/api/agents/tape")
async def api_agents_tape(conversation_id: int, after: int = 0):
    """Live UI-stream events of the agent run currently executing in this
    conversation, for the open chat's telemetry tape. `after` resumes a
    poll without re-fetching events the client already has."""
    return scheduler_mod.tape_snapshot(conversation_id, after)


@app.get("/api/agents")
async def api_agents(workspace: str | None = None):
    """All agents (or one workspace's), each with its standing instructions
    and live run state (last_status drives the failure/success toasts)."""
    views = []
    for a in await db_list_agents(workspace):
        views.append(await _agent_view(a))
    rc, rb = scheduler_mod.get_retry_settings()
    return {"agents": views, "retry": {"retry_count": rc, "retry_backoff_minutes": rb}}


@app.post("/api/agents")
async def api_agents_add(body: AgentBody):
    """Create an agent + its pinned chat (chat_type='agent'). The first
    fire is never immediate: it lands after the first interval / at the
    next clock slot."""
    from backend.agent.loop import agent_is_running

    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    if body.approval_policy not in scheduler_mod.VALID_POLICIES:
        raise HTTPException(status_code=400, detail="approval_policy must be sandbox-only or autonomous")
    stype, spec = _validate_schedule(body.schedule_type, body.schedule_spec)
    conv_id = await create_conversation(
        title=body.name.strip(), workspace=body.workspace or None, chat_type="agent"
    )
    record = await db_create_agent({
        "id": _new_agent_id(),
        "workspace": body.workspace,
        "name": body.name.strip(),
        "prompt": body.prompt.strip(),
        "schedule_type": stype,
        "schedule_spec": spec,
        "approval_policy": body.approval_policy,
        "model": body.model.strip(),
        "effort": body.effort.strip(),
        "memory_enabled": int(body.memory_enabled),
        "allow_ask_user": int(body.allow_ask_user),
        "retention": max(0, int(body.retention or 0)),
        "notify_on_success": int(body.notify_on_success),
        "enabled": int(body.enabled),
        "conversation_id": conv_id,
        "next_fire_at": scheduler_mod.compute_next_fire(stype, spec).isoformat(timespec="seconds"),
    })
    await scheduler_mod.ensure_scheduled()
    return await _agent_view(record)


class AgentModelEffort(BaseModel):
    """#51/#76 write-through body: the pinned chat's header selectors."""
    model: str = ""
    effort: str = ""


@app.patch("/api/agents/{agent_id}/model-effort")
async def api_agents_model_effort(agent_id: str, body: AgentModelEffort):
    """#51/#76: the header selectors of an agent-pinned chat write through
    to the owning agent's model/effort — the chat can never silently
    diverge from the agent that owns it. A targeted field patch, NOT the
    full-record replace (which would recompute next_fire from now); a live
    run refuses, matching the move-chat contract."""
    from backend.agent.loop import agent_is_running

    existing = await db_get_agent(agent_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="agent not found")
    conv_id = existing.get("conversation_id")
    if conv_id and agent_is_running(conv_id):
        raise HTTPException(
            status_code=409,
            detail="the agent is running — stop it before changing its model/effort",
        )
    record = await db_update_agent(
        agent_id,
        {"model": body.model.strip(), "effort": body.effort.strip()},
    )
    return await _agent_view(record)


@app.patch("/api/agents/{agent_id}")
async def api_agents_update(agent_id: str, body: AgentBody):
    """Replace the agent's definition (the dialogue edits the whole record).
    A schedule edit recomputes next_fire from now — never immediately."""
    from backend.agent.loop import agent_is_running

    existing = await db_get_agent(agent_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if body.approval_policy not in scheduler_mod.VALID_POLICIES:
        raise HTTPException(status_code=400, detail="approval_policy must be sandbox-only or autonomous")
    stype, spec = _validate_schedule(body.schedule_type, body.schedule_spec)
    fields = {
        "workspace": body.workspace,
        "name": body.name.strip(),
        "prompt": body.prompt.strip(),
        "schedule_type": stype,
        "schedule_spec": spec,
        "approval_policy": body.approval_policy,
        "model": body.model.strip(),
        "effort": body.effort.strip(),
        "memory_enabled": int(body.memory_enabled),
        "allow_ask_user": int(body.allow_ask_user),
        "retention": max(0, int(body.retention or 0)),
        "notify_on_success": int(body.notify_on_success),
        "enabled": int(body.enabled),
        # A schedule edit restarts the clock from now (never immediate).
        "next_fire_at": scheduler_mod.compute_next_fire(stype, spec).isoformat(timespec="seconds"),
    }
    record = await db_update_agent(agent_id, fields)
    if record["name"] != existing["name"] and existing.get("conversation_id"):
        # Keep the pinned chat's title in sync with the agent name.
        await update_conversation(existing["conversation_id"], title=record["name"])
    if existing.get("enabled") and not record.get("enabled"):
        # Pausing a mid-run agent stops that run too: the user unchecking
        # "enabled" expects the agent to go quiet now, not after the current
        # turn winds down (the run settles through its normal cancel path).
        conv_id = record.get("conversation_id")
        if conv_id and agent_is_running(conv_id):
            scheduler_mod.cancel_agent_run(conv_id)
            scheduler_mod.clear_retry_state(agent_id)
    await scheduler_mod.ensure_scheduled()
    return await _agent_view(record)


@app.delete("/api/agents/{agent_id}")
async def api_agents_remove(agent_id: str, delete_chat: bool = True):
    """Delete an agent. Per spec the pinned chat goes too — but only when
    the caller asked (the UI confirms first); delete_chat=false keeps the
    transcript as a normal chat."""
    existing = await db_get_agent(agent_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="agent not found")
    await db_delete_agent(agent_id)
    conv_id = existing.get("conversation_id")
    if delete_chat and conv_id:
        # adr/0003: the agent's pinned chat ends its worktree session too
        # (same live-run guard as chat deletion).
        from backend.agent import worktrees as _wt
        from backend.agent.loop import agent_is_running

        if not agent_is_running(conv_id):
            await _wt.release_session(str(conv_id), why="agent deleted")
        await delete_conversation(conv_id)
    return {"ok": True}


@app.post("/api/agents/{agent_id}/run")
async def api_agents_run(agent_id: str):
    """Run an agent right now ("Run now" covers testing; the regular
    schedule advances from this fire)."""
    agent = await db_get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    outcome = await scheduler_mod.fire_agent(agent)
    if outcome == "gone":
        raise HTTPException(status_code=409, detail="agent chat was deleted")
    if outcome == "disabled":
        raise HTTPException(status_code=409, detail="agent is disabled (resume it to run)")
    if outcome == "busy":
        raise HTTPException(status_code=409, detail="agent chat is mid-turn")
    return {"ok": True}


class InstructionBody(BaseModel):
    content: str


@app.post("/api/agents/{agent_id}/instructions")
async def api_instruction_add(agent_id: str, body: InstructionBody):
    """Standing instructions: typed messages in an agent chat land here —
    they never trigger a run; they ride along at every fire."""
    if await db_get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail="agent not found")
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    row = await add_instruction(agent_id, content)
    return row


class InstructionUpdate(BaseModel):
    content: str


@app.patch("/api/agents/{agent_id}/instructions/{instruction_id}")
async def api_instruction_update(agent_id: str, instruction_id: int, body: InstructionUpdate):
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    if not await update_instruction(instruction_id, content):
        raise HTTPException(status_code=404, detail="instruction not found")
    return {"ok": True}


@app.delete("/api/agents/{agent_id}/instructions/{instruction_id}")
async def api_instruction_delete(agent_id: str, instruction_id: int):
    if not await delete_instruction(instruction_id):
        raise HTTPException(status_code=404, detail="instruction not found")
    return {"ok": True}


class AgentRetryBody(BaseModel):
    retry_count: int
    retry_backoff_minutes: int


@app.put("/api/agents/retry")
async def api_agents_retry(body: AgentRetryBody):
    """Global retry preference (Settings): failed fires backoff up to N times."""
    scheduler_mod.save_retry_settings(body.retry_count, body.retry_backoff_minutes)
    return {"ok": True, "retry": scheduler_mod.get_retry_settings()}


# ---- Provider / model discovery ----

from fastapi import HTTPException

from backend.agent import providers


@app.get("/api/providers")
async def api_providers():
    """Available provider presets (local + hosted)."""
    return {
        name: {**preset, "api_key": "***" if preset["needs_api_key"] else ""}
        for name, preset in providers.PRESETS.items()
    }


class ModelsRequest(BaseModel):
    api_base: str
    api_key: str | None = None


@app.post("/api/models")
async def api_list_models(body: ModelsRequest):
    """List models from an OpenAI-compatible endpoint."""
    return await providers.list_models(body.api_base, body.api_key or "")


@app.get("/api/models/available")
async def api_available_models():
    """Models offered by every configured provider, queried in parallel.

    Returns {'providers': {name: {models|error}}, 'active_provider',
    'model'}. API keys stay server-side.
    """
    cfg = load_config()
    result = await providers.list_all_models(cfg["providers"])
    result["active_provider"] = cfg["active_provider"]
    result["model"] = cfg["model"]
    return result


# ---- File tree / preview (for the left panel) ----

from pathlib import Path as _Path

from backend.agent import remote as remote_mod
from backend.agent.tools import IGNORED_DIRS, read_file, resolve_path, workspace_root


TREE_DEPTH = 2  # levels returned eagerly; deeper levels load on expand


def _list_dir(dirpath: _Path, root: _Path, depth: int) -> list:
    """One directory's entries, recursing to `depth` more levels. Dirs at the
    cutoff come back with lazy=True (no children) — the UI fetches those on
    expand via /api/files/children. Runs in a worker thread: a synchronous
    walk of a big tree would otherwise freeze the event loop (it did —
    config reads queued behind whole-home-dir walks)."""
    entries = []
    try:
        children = sorted(dirpath.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return entries
    for child in children[:500]:
        if child.name in IGNORED_DIRS or child.name.startswith("."):
            continue
        rel = child.relative_to(root).as_posix()
        if child.is_dir():
            if depth > 1:
                entries.append({"name": child.name, "path": rel, "type": "dir", "children": _list_dir(child, root, depth - 1)})
            else:
                entries.append({"name": child.name, "path": rel, "type": "dir", "lazy": True})
        else:
            entries.append({"name": child.name, "path": rel, "type": "file"})
    return entries


def _walk_in_thread(fn, *args):
    import asyncio

    return asyncio.to_thread(fn, *args)


@app.get("/api/files")
async def api_file_tree(workspace: str):
    """Workspace file tree, TREE_DEPTH levels eager (lazy beyond).

    While a remote session is active the call is proxied to the host, so
    the FilesPanel transparently shows the host's workspace."""
    host = _workspace_host(workspace)
    if host is not None:
        res = await host.proxy(
            "GET", "/api/files", params={"workspace": _host_ws(host, workspace)}
        )
        return _proxy_result(res)

    root = workspace_root(workspace)
    if not root.exists():
        raise HTTPException(status_code=400, detail="workspace does not exist")
    tree = await _walk_in_thread(_list_dir, root, root, TREE_DEPTH)
    return {"root": str(root), "tree": tree}


@app.get("/api/files/children")
async def api_file_children(workspace: str, path: str):
    """Children of a single directory (lazy tree expansion). One level;
    nested dirs come back lazy. Proxied like the rest while connected."""
    host = _workspace_host(workspace)
    if host is not None:
        res = await host.proxy(
            "GET",
            "/api/files/children",
            params={"workspace": _host_ws(host, workspace), "path": path},
        )
        return _proxy_result(res)

    root = workspace_root(workspace)
    dirpath = (root / path).resolve()
    if dirpath != root and root not in dirpath.parents:
        raise HTTPException(status_code=400, detail="path escapes workspace")
    entries = await _walk_in_thread(_list_dir, dirpath, root, 1)
    return {"entries": entries}


class PreviewRequest(BaseModel):
    workspace: str
    path: str
    owner_id: str | None = None
    start_line: int | None = None
    end_line: int | None = None


@app.post("/api/files/preview")
async def api_file_preview(body: PreviewRequest):
    host = _workspace_host(body.workspace)
    if host is not None:
        res = await host.proxy(
            "POST",
            "/api/files/preview",
            json_body={**body.model_dump(), "workspace": _host_ws(host, body.workspace)},
        )
        return _proxy_result(res)
    result = await read_file(
        body.workspace, body.path, start_line=body.start_line, end_line=body.end_line
    )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.delete("/api/files")
async def api_delete_file(workspace: str, path: str):
    """Delete a file from the workspace (file-tree context menu)."""
    host = _workspace_host(workspace)
    if host is not None:
        res = await host.proxy(
            "DELETE",
            "/api/files",
            params={"workspace": _host_ws(host, workspace), "path": path},
        )
        return _proxy_result(res)

    from backend.agent.tools import delete_file

    result = await delete_file(workspace, path)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


def _workspace_host(workspace: str, owner_id: str | None = None):
    """Resolve an explicit remote owner; ordinary paths remain local."""
    ns = remote_mod.parse_ns(workspace)
    if ns is None and owner_id is None:
        return None
    host_id = ns[0] if ns is not None else owner_id
    if ns is not None and remote_mod.get_remote(host_id) is None:
        raise HTTPException(status_code=400, detail="that workspace belongs to a different remote host")
    host = remote_mod.get_remote(host_id)
    if host is None:
        raise HTTPException(status_code=503, detail=f"remote device {host_id} is offline or not connected")
    if owner_id and owner_id != host_id:
        raise HTTPException(status_code=400, detail="workspace owner does not match its namespace")
    return host


def _host_ws(host, workspace: str) -> str:
    """Strip only this host namespace; reject foreign owners."""
    ns = remote_mod.parse_ns(workspace)
    if ns is None:
        return workspace
    hid, path = ns
    if hid != host.host_id:
        raise HTTPException(status_code=400, detail="that workspace belongs to a different remote host")
    return path

def _proxy_result(res):
    """Unwrap a proxied host response; surface host errors as HTTP errors."""
    if res.status_code != 200:
        try:
            detail = res.json().get("detail") or res.text[:200]
        except ValueError:
            detail = res.text[:200]
        raise HTTPException(status_code=res.status_code or 502, detail=detail)
    return res.json()


# ---- Markdown export ----

import json as _json


@app.get("/api/conversations/{conversation_id}/export")
async def api_export_conversation(conversation_id: int):
    """Export a conversation as a Markdown transcript."""
    conv = await get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    rows = await get_messages(conversation_id)

    lines = [f"# {conv['title']}", ""]
    for r in rows:
        role = r["role"]
        if role == "tool":
            meta = (r.get("tool_calls") or [{}])[0]
            name = meta.get("name", "tool") if isinstance(meta, dict) else "tool"
            lines += [f"**🔧 tool: {name}**", "", "```json", r["content"], "```", ""]
        elif role == "assistant":
            lines += [f"**🤖 assistant**", "", r["content"] or "", ""]
            for tc in r.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id") and not tc.get("name"):
                    fn = tc.get("function") or {}
                    try:
                        args = _json.dumps(_json.loads(fn.get("arguments") or "{}"), indent=2)
                    except _json.JSONDecodeError:
                        args = fn.get("arguments", "")
                    lines += [f"**🤖 tool call: {fn.get('name', '?')}**", "", "```json", args, "```", ""]
        else:
            lines += [f"**🧑 user**", "", r["content"], ""]

    from fastapi.responses import Response

    content = "\n".join(lines)
    safe_title = "".join(c for c in conv["title"] if c.isalnum() or c in " -_").strip() or "conversation"
    return Response(
        content=content,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.md"'},
    )


@app.get("/api/config")
async def api_get_config():
    cfg = load_config()
    # Only reveal whether a key is set — never any part of it
    masked = {
        name: {**p, "api_key": "set" if p.get("api_key") else ""}
        for name, p in cfg["providers"].items()
    }
    return {
        "providers": masked,
        "active_provider": cfg["active_provider"],
        "api_base": cfg["api_base"],
        "api_key": "set" if cfg.get("api_key") else "",
        "model": cfg["model"],
        "temperature": cfg.get("temperature"),
        "max_tokens": cfg.get("max_tokens"),
        "max_steps": cfg.get("max_steps"),
        # Reasoning effort (#6): "" = don't send the param to the provider.
        "reasoning_effort": cfg.get("reasoning_effort") or "",
        "last_workspace": cfg.get("last_workspace") or "",
        # Interface scale (CSS zoom): 1.0 = the terminal-grade default ramp.
        "ui_scale": cfg.get("ui_scale", 1.0),
        # Voice settings; the cloud key is masked like provider keys.
        "voice": {
            **(cfg.get("voice") or {}),
            "cloud_api_key": "set" if (cfg.get("voice") or {}).get("cloud_api_key") else "",
        },
        # LAN hosting block. The passphrase is stored plaintext by design
        # (same posture as provider keys) and shown only in this app's UI.
        "remote": cfg.get("remote") or {},
        # Per-model context-window overrides (Settings edits these).
        "context_window_overrides": cfg.get("context_window_overrides") or {},
        # History compaction (Settings edits these; trigger_tokens is an
        # absolute token threshold, 0 = fraction-of-window only).
        "compaction": {
            "enabled": (cfg.get("compaction") or {}).get("enabled", True),
            "trigger_tokens": (cfg.get("compaction") or {}).get("trigger_tokens", 0),
        },
        # Access mode: ask | plan | full (header control; see
        # PLAN-access-modes.md).
        "access_mode": cfg.get("access_mode", "ask"),
    }


@app.put("/api/config")
async def api_set_config(body: ConfigUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    voice = updates.get("voice")
    if isinstance(voice, dict):
        # Voice updates merge over the stored voice block: the GET view masks
        # the cloud key as "set", so a settings round-trip must never wipe it.
        existing = load_config().get("voice") or {}
        if voice.get("cloud_api_key") in ("set", ""):
            voice.pop("cloud_api_key", None)
        updates["voice"] = {**existing, **voice}
    # Remote block merges the same way: a Settings save that only touches
    # hosting_enabled must not wipe the passphrase.
    remote = updates.get("remote")
    if isinstance(remote, dict):
        existing = load_config().get("remote") or {}
        merged = {**existing, **remote}
        updates["remote"] = merged
    # Interface scale is clamped to the shipped range (Settings offers
    # 100/110/125/150%; anything wilder would break the compact layout).
    if "ui_scale" in updates:
        updates["ui_scale"] = min(1.5, max(1.0, float(updates["ui_scale"] or 1.0)))
    # Context-window overrides: when the key is present it is the
    # authoritative full map (Settings sends everything it shows, so removals
    # persist); when absent the stored map is untouched.
    cwo = updates.get("context_window_overrides")
    if isinstance(cwo, dict):
        merged: dict[str, int] = {}
        for model_id, window in cwo.items():
            if window is None:
                continue
            try:
                w = int(window)
            except (TypeError, ValueError):
                continue
            if w > 0:
                merged[str(model_id)] = w
        updates["context_window_overrides"] = merged
    # Access mode is validated against the shipped set; anything else falls
    # back to "ask" (safe default) rather than 422ing a whole settings save.
    if "access_mode" in updates:
        mode = str(updates["access_mode"] or "").lower()
        updates["access_mode"] = mode if mode in ("ask", "plan", "full") else "ask"
    # Compaction merges over the stored block (a Settings save that only
    # touches enabled must not reset trigger_tokens, and vice versa).
    comp = updates.get("compaction")
    if isinstance(comp, dict):
        existing = load_config().get("compaction") or {}
        merged_c = {**existing, **comp}
        merged_c["enabled"] = bool(merged_c.get("enabled", True))
        try:
            merged_c["trigger_tokens"] = max(int(merged_c.get("trigger_tokens") or 0), 0)
        except (TypeError, ValueError):
            merged_c["trigger_tokens"] = 0
        updates["compaction"] = merged_c
    save_config(updates)
    # Hosting toggles need the mDNS advertiser to follow.
    if isinstance(remote, dict):
        from backend.agent import discovery

        if merged.get("hosting_enabled"):
            discovery.start_advertising(API_PORT)
        else:
            discovery.stop_advertising()
    return {"ok": True}


class ModelPick(BaseModel):
    provider: str
    model: str


class LastWorkspace(BaseModel):
    workspace: str


@app.post("/api/config/active-model")
async def api_set_active_model(body: ModelPick):
    """Selecting a model from a provider's dropdown group makes that
    provider active and remembers the model it was last used with."""
    set_active_model(body.provider, body.model)
    return {"ok": True}


@app.post("/api/config/last-workspace")
async def api_set_last_workspace(body: LastWorkspace):
    """Remember the workspace so the sidebar restores it after a restart."""
    set_last_workspace(body.workspace)
    return {"ok": True}


# ---- Voice transcription (local whisper.cpp or BYOK cloud) ----

# Raw-bytes upload (audio/wav) instead of multipart on purpose: the client
# is always our own webview, and raw bodies keep python-multipart out of
# the sidecar dependency set.
MAX_AUDIO_BYTES = 25_000_000


@app.get("/api/transcribe/status")
async def api_transcribe_status():
    """What the mic button can use right now, and with which engine."""
    from backend.agent import transcribe

    voice = load_config().get("voice") or {}
    model = transcribe.find_model()
    return {
        "engine": voice.get("engine") or "local",
        "local_available": transcribe.local_available(),
        "local_model": model.name if model else None,
        "cloud_configured": bool(voice.get("cloud_endpoint")),
    }


@app.post("/api/transcribe")
async def api_transcribe(request: Request):
    """Transcribe a 16 kHz mono WAV body; engine per config (fallback:
    local if configured, else cloud if configured, else 503)."""
    from fastapi.responses import JSONResponse

    from backend.agent import transcribe

    import asyncio
    import os

    pcm = await request.body()
    if len(pcm) > MAX_AUDIO_BYTES:
        return JSONResponse({"detail": "recording too large"}, status_code=413)
    if not pcm:
        return JSONResponse({"detail": "empty recording"}, status_code=400)
    voice = load_config().get("voice") or {}
    engine = voice.get("engine") or "local"
    if engine == "local" and not transcribe.local_available():
        # No silent cloud fallback: shipping a voice recording off-machine
        # because the local engine is missing is a local-first violation.
        # The mic button hides itself in this state; if a recording arrives
        # anyway (engine switched after mount), say so plainly.
        return JSONResponse(
            {"detail": "Local transcription engine not found — pick cloud under Settings > Voice dictation, or reinstall."},
            status_code=503,
        )
    try:
        wav_path = transcribe.save_wav(pcm)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    try:
        if engine == "cloud":
            text = await transcribe.transcribe_cloud(
                wav_path,
                voice.get("cloud_endpoint") or "",
                voice.get("cloud_api_key") or "",
                voice.get("cloud_model") or "",
            )
        else:
            text = await asyncio.to_thread(transcribe.transcribe_local, wav_path)
    except Exception as e:  # noqa: BLE001 — surfaced to the composer as a banner
        return JSONResponse({"detail": str(e)}, status_code=502)
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass
    return {"text": text, "language": transcribe.last_language()}


# ---- Text-to-speech: read-aloud of agent responses (Kokoro via sherpa-onnx) ----

class TtsBody(BaseModel):
    text: str
    voice: str | None = None
    speed: float | None = None
    # The utterance's generation, assigned by the frontend and sent with
    # EVERY chunk of that utterance (concurrent prefetch shares it — see
    # /api/tts/stop). Omit it only in hand-made requests (curl/tests).
    epoch: int | None = None


class TtsStopBody(BaseModel):
    """Optional body for the stop handshake: raise the supersede floor only
    up to this utterance generation (0/absent = invalidate everything)."""
    floor: int = 0


@app.get("/api/tts/status")
async def api_tts_status():
    """What the speaker toggle can use right now: model presence, the voice
    list, and the stored read-aloud settings."""
    from backend.agent import speak

    voice = load_config().get("voice") or {}
    return {
        "available": speak.model_available(),
        "model": speak.MODEL_NAME,
        "model_bytes": speak.MODEL_BYTES,
        "voices": speak.ENGLISH_VOICES,
        "default_voice": speak.DEFAULT_VOICE,
        "tts_enabled": bool(voice.get("tts_enabled")),
        "tts_voice": voice.get("tts_voice") or speak.DEFAULT_VOICE,
        "tts_speed": voice.get("tts_speed", 1.0),
        "downloading": speak.download_in_progress(),
    }


@app.post("/api/tts/stop")
async def api_tts_stop(body: TtsStopBody | None = None):
    """Client-side playback stop handshake. The browser owns the audio
    queue; this endpoint raises the supersede floor so any in-flight
    synthesis for the stopped (or older) utterances aborts at their next
    sentence boundary instead of holding the engine for a full chunk.
    floor = the frontend's current utterance generation (in-flight chunks
    always belong to that generation or an older one). floor 0/absent is a
    no-op — nothing can be in flight below the first generation."""
    from backend.agent import speak

    floor = body.floor if body else 0
    if floor > 0:
        speak.ensure_epoch(floor)
    return {"ok": True}


@app.post("/api/tts/download")
async def api_tts_download():
    """Download + unpack the TTS model; streams progress as JSON lines
    ({"stage": "download"|"extract"|"done"|"error", ...}). One download at
    a time — a second concurrent request is refused with 409."""
    import asyncio
    import json

    from backend.agent import speak
    from fastapi.responses import StreamingResponse

    if speak.download_in_progress():
        raise HTTPException(status_code=409, detail="a download is already running")
    if speak.model_available():
        return {"ok": True, "stage": "done"}

    async def gen():
        q: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def progress(p: dict):
            loop.call_soon_threadsafe(q.put_nowait, p)

        task = loop.run_in_executor(None, speak.download_model, progress)
        while True:
            try:
                p = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if task.done():
                    break
                continue
            yield json.dumps(p) + "\n"
            if p.get("stage") in ("done", "error"):
                break
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:  # noqa: BLE001 — the thread reports via progress events
            pass

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/tts/synthesize")
async def api_tts_synthesize(body: TtsBody):
    """One prose chunk in, one WAV out (24 kHz 16-bit mono). Voice/speed
    default to the stored settings. 409 = model not downloaded yet (the
    UI offers the Settings download); 503 = engine failed to start."""
    import asyncio

    from backend.agent import speak
    from fastapi.responses import JSONResponse, Response

    if not speak.model_available():
        return JSONResponse({"detail": "TTS model not downloaded"}, status_code=409)
    text = (body.text or "").strip()
    if not text:
        return JSONResponse({"detail": "empty text"}, status_code=400)
    if len(text) > 5000:
        return JSONResponse({"detail": "text too long — split into sentences"}, status_code=413)
    voice_cfg = load_config().get("voice") or {}
    voice = body.voice or voice_cfg.get("tts_voice") or speak.DEFAULT_VOICE
    speed = body.speed if body.speed is not None else (voice_cfg.get("tts_speed") or 1.0)
    # The utterance's generation comes from the frontend (body.epoch); a
    # hand-made request without one gets epoch 0, which any stop invalidates.
    epoch = body.epoch if body.epoch is not None else 0
    try:
        pcm, rate = await asyncio.to_thread(
            speak.synthesize, text, voice, float(speed), epoch
        )
    except speak.SupersededError:
        # A newer utterance replaced this one; nothing to send.
        return JSONResponse({"detail": "superseded"}, status_code=409)
    except RuntimeError as e:
        return JSONResponse({"detail": str(e)}, status_code=503)
    except Exception as e:  # noqa: BLE001 — surfaced as a spoken-output error
        return JSONResponse({"detail": f"{type(e).__name__}: {e}"}, status_code=500)
    return Response(content=speak.wav_bytes(pcm, rate), media_type="audio/wav")


# ---- Remote hosting: host role (serve other YAAH instances) + client role ----

from backend.agent import discovery, remote as remote_mod
from backend.agent.remote import PROTOCOL_VERSION
from backend.agent import tools as tools_mod


class RemoteExec(BaseModel):
    name: str
    args: dict = {}
    workspace: str = ""


@app.get("/api/remote/info")
async def api_remote_info():
    """Handshake for a connecting client: identity + protocol + target OS."""
    info = remote_mod.host_info()
    info["app_version"] = app.version
    info["display_name"] = discovery._display_name()
    return info


@app.get("/api/remote/conversations")
async def api_remote_conversations():
    """Read host-owned conversation metadata for an authenticated client."""
    rows = await list_conversations()
    for row in rows:
        row.pop("system_prompt_override", None)
        row.pop("context_tokens", None)
        row.pop("context_model", None)
    return rows


@app.get("/api/remote/conversations/{conversation_id}/messages")
async def api_remote_conversation_messages(conversation_id: int):
    """Read host-owned transcript for an authenticated client."""
    if await get_conversation(conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return await get_messages(conversation_id)


@app.post("/api/remote/exec")
async def api_remote_exec(body: RemoteExec):
    """Execute one workspace tool on THIS host, in the workspace the client
    selected (empty = this host's default workspace = home). Only
    reachable with the passphrase (the X-Yaah-Remote middleware above
    enforces it for every marker-carrying request), and only for tools
    in REMOTE_TOOLS — the host never runs anything else on behalf of a
    remote peer. Dispatches through EXECUTORS directly, NOT execute_tool:
    the host-side executor must never consult this host's own remote
    session, or a host that is also connected out would forward the call
    in a loop."""
    fn = tools_mod.EXECUTORS.get(body.name)
    if body.name not in remote_mod.REMOTE_TOOLS or fn is None:
        raise HTTPException(status_code=400, detail=f"{body.name} is not a remote-capable tool")
    # Passed through as-is: every workspace the client can name here came
    # from this host's registry (already normalized by /api/workspaces on
    # THIS host), so re-normalizing would just re-apply the wrong OS's rules.
    try:
        return await fn(workspace=body.workspace.strip(), **body.args)
    except TypeError as e:
        return {"error": f"Bad arguments for {body.name}: {e}"}
    except Exception as e:  # noqa: BLE001 — mirror execute_tool's never-raise
        return {"error": f"{type(e).__name__}: {e}"}


class RemoteConnect(BaseModel):
    url: str
    passphrase: str = ""


class RemoteDeviceConnect(BaseModel):
    url: str
    passphrase: str = ""


class RemoteDeviceReconnect(BaseModel):
    passphrase: str = ""


def _remote_device_profiles() -> list[dict]:
    from backend.agent.config import load_config
    profiles = load_config().get("remote_devices") or []
    return [dict(profile) for profile in profiles if isinstance(profile, dict)]


def _save_remote_device_profiles(profiles: list[dict]) -> None:
    from backend.agent.config import save_config
    safe = [{key: value for key, value in profile.items() if key != "passphrase"} for profile in profiles]
    save_config({"remote_devices": safe})


def _public_remote_device(profile: dict) -> dict:
    host_id = profile["host_id"]
    session = remote_mod.get_remote(host_id)
    status = profile.get("status", "offline")
    if session is not None and status != "error":
        status = "online"
    return {"host_id": host_id, "url": profile.get("url", ""),
            "name": (session.name if session else profile.get("name")) or profile.get("url", host_id),
            "os": session.info.get("os") if session else profile.get("os"), "status": status,
            "workspaces": profile.get("cached_workspaces") or []}


@app.get("/api/remote/devices/{host_id}/conversations")
async def api_remote_device_conversations(host_id: str):
    """Refresh and return cached conversation metadata for one saved device."""
    if not any(profile.get("host_id") == host_id for profile in _remote_device_profiles()):
        raise HTTPException(status_code=404, detail="remote device not found")
    session = remote_mod.get_remote(host_id)
    if session is not None:
        try:
            remote_rows = _proxy_result(await session.proxy("GET", "/api/remote/conversations"))
            for row in remote_rows:
                conv_id = str(row["id"])
                messages = _proxy_result(await session.proxy(
                    "GET", f"/api/remote/conversations/{conv_id}/messages"
                ))
                await upsert_remote_conversation(host_id, row, messages)
        except (httpx.HTTPError, HTTPException) as exc:
            cached = await list_remote_conversations(host_id)
            if not cached:
                raise HTTPException(status_code=503, detail="remote device unavailable and no cached conversations") from exc
    cached = await list_remote_conversations(host_id)
    return {"conversations": cached, "status": "online" if session else "cached"}


@app.get("/api/remote/devices/{host_id}/conversations/{conversation_id}/messages")
async def api_remote_device_messages(host_id: str, conversation_id: str):
    """Refresh online, otherwise serve the owner-scoped cached transcript."""
    if not any(profile.get("host_id") == host_id for profile in _remote_device_profiles()):
        raise HTTPException(status_code=404, detail="remote device not found")
    session = remote_mod.get_remote(host_id)
    if session is not None:
        try:
            rows = _proxy_result(await session.proxy("GET", "/api/remote/conversations"))
            conversation = next((row for row in rows if str(row.get("id")) == str(conversation_id)), None)
            if conversation is None:
                raise HTTPException(status_code=404, detail="conversation not found")
            messages = _proxy_result(await session.proxy(
                "GET", f"/api/remote/conversations/{conversation_id}/messages"
            ))
            await upsert_remote_conversation(host_id, conversation, messages)
        except (httpx.HTTPError, HTTPException):
            pass
    messages = await get_remote_messages(host_id, conversation_id)
    if messages is None:
        raise HTTPException(status_code=404, detail="remote conversation is not cached")
    return messages


def _validate_remote_image_rel(rel: str) -> str:
    """Accept only POSIX-style relative image paths; reject traversal before proxying."""
    from pathlib import PurePosixPath

    if (not rel or rel.startswith(("/", "\\")) or "\\" in rel or "\x00" in rel):
        raise HTTPException(status_code=400, detail="invalid image path")
    parts = rel.split("/")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise HTTPException(status_code=400, detail="invalid image path")
    path = PurePosixPath(rel)
    if path.is_absolute() or path.as_posix() != rel:
        raise HTTPException(status_code=400, detail="invalid image path")
    return rel


@app.get("/api/remote/devices/{host_id}/images/{rel:path}")
async def api_remote_device_image(host_id: str, rel: str):
    """Retrieve media through the saved owner device without exposing its credentials."""
    from urllib.parse import quote

    from fastapi.responses import Response

    rel = _validate_remote_image_rel(rel)
    if not any(profile.get("host_id") == host_id for profile in _remote_device_profiles()):
        raise HTTPException(status_code=404, detail="remote device not found")
    session = remote_mod.get_remote(host_id)
    if session is None or getattr(session, "host_id", None) != host_id:
        raise HTTPException(status_code=503, detail="remote device is offline")
    try:
        # Quote each untrusted path character while preserving already-validated
        # separators; never accept a caller-supplied URL or forward client headers.
        remote_path = "/api/images/" + quote(rel, safe="/")
        res = await session.proxy("GET", remote_path)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="remote image unavailable") from exc
    if res.status_code == 404:
        raise HTTPException(status_code=404, detail="image not found")
    if res.status_code != 200:
        # Do not relay remote response text/headers, which may contain secrets.
        raise HTTPException(status_code=502, detail="remote image retrieval failed")
    content_type = res.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}:
        raise HTTPException(status_code=502, detail="remote endpoint returned unsupported media")
    return Response(
        content=res.content,
        media_type=content_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/remote/devices")
async def api_remote_devices():
    profiles = _remote_device_profiles()
    for profile in profiles:
        session = remote_mod.get_remote(profile.get("host_id", ""))
        if session is not None:
            try:
                rows = _proxy_result(await session.proxy("GET", "/api/workspaces/local"))
                profile["cached_workspaces"] = [{**row, "path": remote_mod.ns_path(session.host_id, row.get("path")),
                    "owner_id": session.host_id, "device_status": "online"} for row in rows]
                profile.update({"name": session.name, "os": session.info.get("os"), "status": "online"})
            except (httpx.HTTPError, HTTPException):
                profile["status"] = "error"
                profile["cached_workspaces"] = [{**row, "owner_id": session.host_id, "device_status": "error"}
                    for row in profile.get("cached_workspaces", [])]
        else:
            profile["status"] = "offline"
    _save_remote_device_profiles(profiles)
    return {"devices": [_public_remote_device(profile) for profile in profiles]}


@app.post("/api/remote/devices")
async def api_add_remote_device(body: RemoteDeviceConnect):
    host = await _connect_remote_session(body.url, body.passphrase, make_active=False)
    profiles = _remote_device_profiles()
    profile = next((item for item in profiles if item.get("host_id") == host.host_id), None)
    if profile is None:
        profile = {"host_id": host.host_id, "cached_workspaces": []}
        profiles.append(profile)
    profile.update({"url": host.url, "name": host.name, "os": host.info.get("os"), "status": "online"})
    _save_remote_device_profiles(profiles)
    return _public_remote_device(profile)


@app.post("/api/remote/devices/{host_id}/connect")
async def api_connect_remote_device(host_id: str, body: RemoteDeviceReconnect):
    profiles = _remote_device_profiles()
    profile = next((item for item in profiles if item.get("host_id") == host_id), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="remote device not found")
    host = await _connect_remote_session(profile.get("url", ""), body.passphrase, make_active=False)
    if host.host_id != host_id:
        raise HTTPException(status_code=409, detail="the URL now identifies a different remote device")
    profile.update({"name": host.name, "os": host.info.get("os"), "status": "online"})
    _save_remote_device_profiles(profiles)
    return _public_remote_device(profile)


@app.post("/api/remote/devices/{host_id}/disconnect")
async def api_disconnect_remote_device(host_id: str):
    remote_mod.unregister_remote(host_id)
    profiles = _remote_device_profiles()
    for profile in profiles:
        if profile.get("host_id") == host_id:
            profile["status"] = "offline"
    _save_remote_device_profiles(profiles)
    return {"ok": True}


@app.delete("/api/remote/devices/{host_id}")
async def api_remove_remote_device(host_id: str):
    remote_mod.unregister_remote(host_id)
    _save_remote_device_profiles([profile for profile in _remote_device_profiles() if profile.get("host_id") != host_id])
    return {"ok": True}


async def _connect_remote_session(url: str, passphrase: str, *, make_active: bool):
    from urllib.parse import urlsplit
    import re
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="device URL must be an HTTP or HTTPS address")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="device URLs must not contain embedded credentials")
    url = url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(f"{url}/api/remote/info")
        response.raise_for_status()
        info = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(status_code=502, detail=f"host unreachable: {error}")
    if info.get("protocol") != PROTOCOL_VERSION:
        raise HTTPException(status_code=409, detail="Incompatible YAAH versions: update both instances.")
    if info.get("instance_id") == remote_mod.INSTANCE_ID:
        raise HTTPException(status_code=400, detail="refusing to connect to this same instance")
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            verified = await client.get(f"{url}/api/remote/verify", headers={"X-Yaah-Remote": "1", "X-Yaah-Passphrase": passphrase})
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"host unreachable: {error}")
    if verified.status_code == 401:
        raise HTTPException(status_code=401, detail="wrong passphrase")
    if verified.status_code != 200:
        raise HTTPException(status_code=502, detail=f"host verify failed ({verified.status_code})")
    host_id = str(info.get("host_id") or "")
    if not host_id:
        host_id = "h-" + (info.get("hostname") or url).lower().replace(" ", "-")[:120]
        info["host_id"] = host_id
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", host_id):
        raise HTTPException(status_code=502, detail="remote host returned an invalid device identity")
    existing = remote_mod.get_remote(host_id)
    if existing is not None and existing.url != url:
        raise HTTPException(status_code=409, detail="device identity is already connected at a different URL")
    host = remote_mod.RemoteSession(url, passphrase, info, app_version=info.get("app_version", ""))
    remote_mod.register_remote(host, make_active=make_active)
    return host


@app.post("/api/remote/connect")
async def api_remote_connect(body: RemoteConnect):
    host = await _connect_remote_session(body.url, body.passphrase, make_active=True)
    return remote_status_dict(host)





@app.get("/api/remote/discover")
async def api_remote_discover():
    """mDNS sweep for hosts on this LAN (blocking sweep, ~2.5s)."""
    import asyncio

    hosts = await asyncio.to_thread(discovery.browse)
    return {"hosts": hosts}


@app.get("/api/remote/verify")
async def api_remote_verify():
    """Authenticated probe for connecting clients: carries no data - it
    exists so /api/remote/connect can confirm the passphrase BEFORE the
    session is accepted (the info handshake is deliberately open, so
    without this a wrong passphrase would "connect" green and only 401
    later on every proxied call)."""
    return {"ok": True}


@app.post("/api/remote/disconnect")
async def api_remote_disconnect():
    remote_mod.clear_remote()
    return {"ok": True}


def remote_status_dict(host: remote_mod.RemoteSession | None = None):
    host = host if host is not None else remote_mod.get_remote()
    if host is None:
        return {"connected": False}
    return {
        "connected": True,
        "url": host.url,
        "name": host.name,
        "host_id": host.host_id,
        "os": host.info.get("os"),
        "app_version": host.app_version,
        "workspace_root": host.info.get("workspace_root"),
    }


@app.get("/api/remote/status")
async def api_remote_status():
    # Legacy status reflects only the old explicit host switcher. Device
    # profiles and their sessions do not set a global execution mode.
    return remote_status_dict() if remote_mod._active_host_id else {"connected": False}
