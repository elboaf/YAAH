"""AI Coding Agent — FastAPI backend entry point.

Runs as an embedded subprocess inside the Tauri desktop app (or standalone
during development). All model API calls, tool execution, and persistence
flow through this server.
"""
from contextlib import asynccontextmanager

import httpx

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

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
    from backend.agent import discovery
    from backend.agent.config import load_config

    if (load_config().get("remote") or {}).get("hosting_enabled", True):
        discovery.start_advertising(API_PORT)
    yield
    discovery.stop_advertising()


# The sidecar always serves on this port (backend_entry.py, lib.rs spawn).
API_PORT = 8765

app = FastAPI(title="AI Coding Agent", version="0.7.2", lifespan=lifespan)

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
        expected = (load_config().get("remote") or {}).get("passphrase") or ""
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
    touch_workspace,
    update_conversation,
    delete_conversation,
    upsert_workspace,
)


class NewConversation(BaseModel):
    title: str = "New Task"
    workspace: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = None
    workspace: str | None = None
    system_prompt_override: str | None = None


class NewMessage(BaseModel):
    role: str
    content: str
    tool_calls: list | None = None
    tool_call_id: str | None = None


@app.post("/api/conversations")
async def api_create_conversation(body: NewConversation):
    cid = await create_conversation(body.title, body.workspace)
    return {"id": cid}


# ---- Workspace registry ----


class NewWorkspace(BaseModel):
    path: str


@app.get("/api/workspaces")
async def api_list_workspaces():
    """Registry rows for the sidebar dropdown and grouped list.

    While a remote session is active this mirrors the HOST's registry;
    every path comes back namespaced ('remote:<hid>:<path>') so remote
    groups can never collide with same-named local paths on this machine.
    """
    host = remote_mod.get_remote()
    if host is not None:
        res = await host.proxy("GET", "/api/workspaces")
        rows = _proxy_result(res)
        for r in rows:
            r["path"] = remote_mod.ns_path(host.host_id, r.get("path"))
        return rows
    return [
        r for r in await list_workspaces()
        if remote_mod.parse_ns(r["path"]) is None
    ]


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
    """Register a folder (resolved + deduped) and select it implicitly.
    While connected, the folder is registered on the HOST: a namespaced
    path is stripped, a raw path is taken as-is (the host validates that
    it exists)."""
    import os

    host = remote_mod.get_remote()
    if host is not None:
        raw = remote_mod.parse_ns(body.path)
        res = await host.proxy(
            "POST", "/api/workspaces", json_body={"path": raw[1] if raw else body.path}
        )
        row = _proxy_result(res)
        row["path"] = remote_mod.ns_path(host.host_id, row.get("path"))
        return row
    ws = await upsert_workspace(body.path)
    ws["exists"] = True if ws["path"] is None else os.path.isdir(ws["path"])
    return ws


@app.delete("/api/workspaces/{workspace_id}")
async def api_delete_workspace(workspace_id: int):
    """Remove a workspace; its conversations relocate to Default (host-side
    registry and host-side relocation while connected)."""
    from fastapi import HTTPException

    host = remote_mod.get_remote()
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
    return await list_conversations()


@app.get("/api/conversations/{conversation_id}")
async def api_get_conversation(conversation_id: int):
    conv = await get_conversation(conversation_id)
    if conv is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


@app.patch("/api/conversations/{conversation_id}")
async def api_update_conversation(conversation_id: int, body: ConversationUpdate):
    ok = await update_conversation(
        conversation_id,
        title=body.title,
        workspace=body.workspace,
        system_prompt_override=body.system_prompt_override,
    )
    return {"ok": ok}


@app.delete("/api/conversations/{conversation_id}")
async def api_delete_conversation(conversation_id: int):
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
    voice: dict | None = None
    remote: dict | None = None


@app.post("/api/agent/{conversation_id}")
async def api_agent_turn(conversation_id: int, body: AgentTurn):
    """Run one agent turn; stream JSON-line events."""
    # Every turn runs in the workspace the UI has selected: remember it so the
    # sidebar restores the same folder after an app restart, and touch the
    # registry row so the dropdown/group order reflects recent activity.
    set_last_workspace(body.workspace)
    await touch_workspace(body.workspace or None)
    # Attached images: decode data URLs to files on disk up front; only the
    # rel paths travel into the agent loop and the database.
    from backend.agent.imagedata import save_data_url

    image_paths = []
    for data_url in body.images[:4]:  # cap at 4 images per message
        rel = save_data_url(data_url, subdir=str(conversation_id))
        if rel:
            image_paths.append(rel)
    return StreamingResponse(
        run_agent(conversation_id, body.message, body.workspace,
                  image_paths=image_paths, skill_names=body.skills,
                  persist_user=not body.resume),
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

    host = remote_mod.get_remote()
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
    host = remote_mod.get_remote()
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
    host = remote_mod.get_remote()
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
    start_line: int | None = None
    end_line: int | None = None


@app.post("/api/files/preview")
async def api_file_preview(body: PreviewRequest):
    host = remote_mod.get_remote()
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
    host = remote_mod.get_remote()
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


def _host_ws(host, workspace: str) -> str:
    """Translate a client-side workspace string into a raw host path for
    proxying: a namespaced workspace must belong to the CONNECTED host
    (defends against chatting into one host while a stale path points at
    another); anything else passes through (empty = host default)."""
    ns = remote_mod.parse_ns(workspace)
    if ns is None:
        return workspace
    hid, path = ns
    if hid != host.host_id:
        raise HTTPException(
            status_code=400,
            detail="that workspace belongs to a different remote host",
        )
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
        "last_workspace": cfg.get("last_workspace") or "",
        # Voice settings; the cloud key is masked like provider keys.
        "voice": {
            **(cfg.get("voice") or {}),
            "cloud_api_key": "set" if (cfg.get("voice") or {}).get("cloud_api_key") else "",
        },
        # LAN hosting block. The passphrase is stored plaintext by design
        # (same posture as provider keys) and shown only in this app's UI.
        "remote": cfg.get("remote") or {},
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
    return {"text": text}

# ---- Remote hosting: host role (serve other YAAH instances) + client role ----

from backend.agent import discovery, remote as remote_mod
from backend.agent.remote import PROTOCOL_VERSION
from backend.agent import tools as tools_mod


class RemoteExec(BaseModel):
    name: str
    args: dict = {}


@app.get("/api/remote/info")
async def api_remote_info():
    """Handshake for a connecting client: identity + protocol + target OS."""
    info = remote_mod.host_info()
    info["app_version"] = app.version
    info["display_name"] = discovery._display_name()
    return info


@app.post("/api/remote/exec")
async def api_remote_exec(body: RemoteExec):
    """Execute one workspace tool in THIS host's default workspace. Only
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
    try:
        return await fn(workspace="", **body.args)
    except TypeError as e:
        return {"error": f"Bad arguments for {body.name}: {e}"}
    except Exception as e:  # noqa: BLE001 — mirror execute_tool's never-raise
        return {"error": f"{type(e).__name__}: {e}"}


class RemoteConnect(BaseModel):
    url: str  # e.g. http://192.168.1.10:8765 (or a tailscale https URL)
    passphrase: str = ""


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


@app.post("/api/remote/connect")
async def api_remote_connect(body: RemoteConnect):
    """Handshake with a host, refuse protocol mismatches and wrong
    passphrases, then make it the active session (workspace tools + files
    proxy route there)."""
    url = body.url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            res = await client.get(f"{url}/api/remote/info")
        res.raise_for_status()
        info = res.json()
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(status_code=502, detail=f"host unreachable: {e}")
    if info.get("protocol") != PROTOCOL_VERSION:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Incompatible YAAH versions: this app speaks protocol "
                f"{PROTOCOL_VERSION}, the host reports {info.get('protocol')}. "
                "Update both instances to matching versions."
            ),
        )
    # Connecting this instance to itself would send every workspace tool
    # and files call in an endless loop back through its own endpoints.
    if info.get("instance_id") == remote_mod.INSTANCE_ID:
        raise HTTPException(status_code=400, detail="refusing to connect to this same instance")
    # Auth check before accepting the session (see /api/remote/verify):
    # the info handshake is open, so without this probe a wrong passphrase
    # would "connect" green and only 401 later on every proxied call.
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            vres = await client.get(
                f"{url}/api/remote/verify",
                headers={"X-Yaah-Remote": "1", "X-Yaah-Passphrase": body.passphrase},
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"host unreachable: {e}")
    if vres.status_code == 401:
        raise HTTPException(status_code=401, detail="wrong passphrase")
    if vres.status_code != 200:
        raise HTTPException(status_code=502, detail=f"host verify failed ({vres.status_code})")
    host = remote_mod.RemoteSession(url, body.passphrase, info, app_version=info.get("app_version", ""))
    remote_mod.set_remote(host)
    return remote_status_dict(host)


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
    return remote_status_dict()
