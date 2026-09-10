"""AI Coding Agent — FastAPI backend entry point.

Runs as an embedded subprocess inside the Tauri desktop app (or standalone
during development). All model API calls, tool execution, and persistence
flow through this server.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.db.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="AI Coding Agent", version="0.1.0", lifespan=lifespan)

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
    get_conversation,
    get_messages,
    list_conversations,
    update_conversation,
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


@app.post("/api/agent/{conversation_id}")
async def api_agent_turn(conversation_id: int, body: AgentTurn):
    """Run one agent turn; stream JSON-line events."""
    # Every turn runs in the workspace the UI has selected: remember it so the
    # sidebar restores the same folder after an app restart.
    set_last_workspace(body.workspace)
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
                  image_paths=image_paths),
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


@app.post("/api/agent/{conversation_id}/cancel")
async def api_agent_cancel(conversation_id: int):
    """Ask a running agent turn to stop after its current step."""
    from backend.agent.loop import cancel_agent

    cancel_agent(conversation_id)
    return {"ok": True}


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
    }


@app.put("/api/config")
async def api_set_config(body: ConfigUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
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