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
    allow_origins=["http://localhost:1420", "tauri://localhost"],
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
    get_messages,
    list_conversations,
)


class NewConversation(BaseModel):
    title: str = "New Task"
    workspace: str | None = None


class NewMessage(BaseModel):
    role: str
    content: str
    tool_calls: list | None = None


@app.post("/api/conversations")
async def api_create_conversation(body: NewConversation):
    cid = await create_conversation(body.title, body.workspace)
    return {"id": cid}


@app.get("/api/conversations")
async def api_list_conversations():
    return await list_conversations()


@app.get("/api/conversations/{conversation_id}/messages")
async def api_get_messages(conversation_id: int):
    return await get_messages(conversation_id)


@app.post("/api/conversations/{conversation_id}/messages")
async def api_add_message(conversation_id: int, body: NewMessage):
    mid = await add_message(conversation_id, body.role, body.content, body.tool_calls)
    return {"id": mid}


# ---- Agent streaming endpoint ----

from fastapi.responses import StreamingResponse

from backend.agent.config import load_config, save_config
from backend.agent.loop import run_agent


class AgentTurn(BaseModel):
    message: str
    workspace: str


class ConfigUpdate(BaseModel):
    api_base: str | None = None
    api_key: str | None = None
    model: str | None = None


@app.post("/api/agent/{conversation_id}")
async def api_agent_turn(conversation_id: int, body: AgentTurn):
    """Run one agent turn; stream JSON-line events."""
    return StreamingResponse(
        run_agent(conversation_id, body.message, body.workspace),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/config")
async def api_get_config():
    cfg = load_config()
    # Mask the key
    key = cfg.get("api_key", "")
    return {**cfg, "api_key": (key[:4] + "..." if key else "")}


@app.put("/api/config")
async def api_set_config(body: ConfigUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    save_config(updates)
    return {"ok": True}