"""AI Coding Agent — FastAPI backend entry point.

Runs as an embedded subprocess inside the Tauri desktop app (or standalone
during development). All model API calls, tool execution, and persistence
flow through this server.
"""
from contextlib import asynccontextmanager

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
    yield


app = FastAPI(title="AI Coding Agent", version="0.6.10", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # The Tauri webview origin differs per platform: tauri://localhost on
    # macOS, http(s)://tauri.localhost on Windows/Linux. Cover vite dev and
    # any tauri origin; the server only ever listens on localhost.
    allow_origin_regex=r"^https?://(localhost|tauri\.localhost)(:\d+)?$|^tauri://localhost$",
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    """Registry rows for the sidebar dropdown and grouped list."""
    return await list_workspaces()


@app.post("/api/workspaces")
async def api_add_workspace(body: NewWorkspace):
    """Register a folder (resolved + deduped) and select it implicitly."""
    import os

    ws = await upsert_workspace(body.path)
    ws["exists"] = True if ws["path"] is None else os.path.isdir(ws["path"])
    return ws


@app.delete("/api/workspaces/{workspace_id}")
async def api_delete_workspace(workspace_id: int):
    """Remove a workspace; its conversations relocate to Default."""
    from fastapi import HTTPException

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
    workspace-sandboxed read_file tool can open it on demand."""
    import os

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

from backend.agent.tools import IGNORED_DIRS, read_file, resolve_path


@app.get("/api/files")
async def api_file_tree(workspace: str):
    """Recursive file tree of the workspace (ignored dirs skipped)."""
    root = _Path(workspace).resolve()
    if not root.exists():
        raise HTTPException(status_code=400, detail="workspace does not exist")

    def build(dirpath: _Path) -> list:
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
                entries.append({"name": child.name, "path": rel, "type": "dir", "children": build(child)})
            else:
                entries.append({"name": child.name, "path": rel, "type": "file"})
        return entries

    return {"root": str(root), "tree": build(root)}


class PreviewRequest(BaseModel):
    workspace: str
    path: str
    start_line: int | None = None
    end_line: int | None = None


@app.post("/api/files/preview")
async def api_file_preview(body: PreviewRequest):
    result = await read_file(
        body.workspace, body.path, start_line=body.start_line, end_line=body.end_line
    )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.delete("/api/files")
async def api_delete_file(workspace: str, path: str):
    """Delete a file from the workspace (file-tree context menu)."""
    from backend.agent.tools import delete_file

    result = await delete_file(workspace, path)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


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
    save_config(updates)
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
        "cloud_configured": bool(voice.get("cloud_endpoint") and voice.get("cloud_api_key")),
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
                voice.get("cloud_model") or "whisper-1",
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