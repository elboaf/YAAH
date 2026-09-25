"""Per-chat model + reasoning effort (#51/#76).

Covers the resolution chain end to end: the model_client precedence table,
the pure turn-scope resolver in main, the stamp-on-upgrade migration, the
PATCH write path (including the agent-chat 409 guard), the write-through
endpoint, and one loop-level test proving overrides ride along per call.
"""
import json

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import _resolve_turn_scope, app
from backend.agent import loop
from backend.agent.model_client import _build_payload, _resolve_call_cfg
from backend.db.database import (
    create_conversation,
    get_conversation,
    update_conversation,
)


# ------------------------------------------------------- model_client precedence


def _cfg(effort=None):
    cfg = {
        "model": "global-m",
        "api_base": "http://p1",
        "api_key": "",
        "providers": {"p1": {"api_base": "http://p1"}, "p2": {"api_base": "http://p2"}},
    }
    if effort is not None:
        cfg["reasoning_effort"] = effort
    return cfg


def test_resolve_model_blank_keeps_global():
    cfg = _resolve_call_cfg(_cfg(), "")
    assert cfg["model"] == "global-m"


def test_resolve_model_bare_id_keeps_provider():
    cfg = _resolve_call_cfg(_cfg(), "m2")
    assert cfg["model"] == "m2"
    assert cfg["api_base"] == "http://p1"


def test_resolve_model_provider_prefix_routes_provider():
    cfg = _resolve_call_cfg(_cfg(), "p2::m3")
    assert cfg["model"] == "m3"
    assert cfg["api_base"] == "http://p2"


def test_resolve_model_unknown_provider_raises():
    from backend.agent.model_client import ModelError

    with pytest.raises(ModelError):
        _resolve_call_cfg(_cfg(), "nope::m")


def test_resolve_effort_blank_inherits_global():
    cfg = _resolve_call_cfg(_cfg("high"), "")
    assert cfg["reasoning_effort"] == "high"


def test_resolve_effort_none_is_explicit_no_param():
    """#51/#76: a stamped '' effort means Default = never send the param,
    even when the global setting would send it."""
    cfg = _resolve_call_cfg(_cfg("high"), effort=None)
    assert cfg["reasoning_effort"] == ""
    assert "reasoning_effort" not in _build_payload(cfg, None, False)


def test_resolve_effort_level_overrides_global():
    cfg = _resolve_call_cfg(_cfg("low"), effort="HIGH")
    assert cfg["reasoning_effort"] == "high"
    payload = _build_payload(cfg, None, False)
    assert payload["reasoning_effort"] == "high"


def test_resolve_effort_none_still_gates_unknown_values():
    cfg = _resolve_call_cfg(_cfg("max"), effort=None)
    assert "reasoning_effort" not in _build_payload(cfg, None, False)


# ------------------------------------------------------------ turn-scope resolver


def test_scope_unstamped_row_falls_back_to_globals():
    model, effort = _resolve_turn_scope({"model": "", "effort": ""})
    assert model == ""
    assert effort is None


def test_scope_normal_chat_stamped_effort_empty_means_no_param():
    """'' in a normal chat row is a DELIBERATE Default -> None sentinel."""
    model, effort = _resolve_turn_scope({"model": "p1::m", "effort": ""})
    assert model == "p1::m"
    assert effort is None


def test_scope_normal_chat_level_applies():
    model, effort = _resolve_turn_scope({"model": "m", "effort": " Medium "})
    assert (model, effort) == ("m", "medium")


def test_scope_agent_chat_resolves_through_agent_inherit_global():
    model, effort = _resolve_turn_scope(None, {"model": "p2::m9", "effort": ""})
    assert (model, effort) == ("p2::m9", "")


def test_scope_agent_chat_level():
    model, effort = _resolve_turn_scope(None, {"model": "", "effort": "high"})
    assert (model, effort) == ("", "high")


# ------------------------------------------------------------------- migration


@pytest.mark.asyncio
async def test_stamp_migration_stamps_blanks_and_skips_agent_rows(monkeypatch):
    """The upgrade stamp: blank normal rows get the then-current globals ONCE
    (never again — '' afterwards is a deliberate Default); agent rows keep ''
    because their scope resolves through the owning agent."""
    import backend.db.database as db_mod

    monkeypatch.setattr(
        "backend.agent.config.load_config",
        lambda: {"model": "p1::g-model", "reasoning_effort": "low"},
    )
    cid = await create_conversation("legacy")
    agent_cid = await create_conversation("agent-row", chat_type="agent")
    # Simulate the pre-upgrade state: rows exist with blank scope columns.
    await update_conversation(cid, model="", effort="")
    await update_conversation(agent_cid, model="", effort="")

    db_mod._stamp_scope_done = False
    db = await db_mod.get_db()
    try:
        await db_mod._stamp_conversation_scopes(db)
    finally:
        await db.close()

    conv = await get_conversation(cid)
    assert conv["model"] == "p1::g-model"
    assert conv["effort"] == "low"
    agent_conv = await get_conversation(agent_cid)
    assert agent_conv["model"] == ""
    assert agent_conv["effort"] == ""

    # Idempotent: the flag keeps the stamp from re-running (a later flip of
    # the globals must NOT rewrite rows that already carry a decision).
    monkeypatch.setattr(
        "backend.agent.config.load_config",
        lambda: {"model": "p2::other", "reasoning_effort": "high"},
    )
    db = await db_mod.get_db()
    try:
        await update_conversation(cid, model="", effort="")
        await db_mod._stamp_conversation_scopes(db)
    finally:
        await db.close()
    conv = await get_conversation(cid)
    assert conv["model"] == ""  # untouched: stamp is one-time


@pytest.mark.asyncio
async def test_create_conversation_explicit_values_win_over_globals(monkeypatch):
    monkeypatch.setattr(
        "backend.agent.config.load_config",
        lambda: {"model": "g", "reasoning_effort": ""},
    )
    cid = await create_conversation("explicit", model="p2::mine", effort="high")
    conv = await get_conversation(cid)
    assert conv["model"] == "p2::mine"
    assert conv["effort"] == "high"


# ------------------------------------------------------------- conversation API


@pytest.mark.asyncio
async def test_patch_model_and_effort_roundtrip():
    cid = await create_conversation("patch")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.patch(
            f"/api/conversations/{cid}", json={"model": "p1::x", "effort": ""}
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
    conv = await get_conversation(cid)
    assert conv["model"] == "p1::x"
    assert conv["effort"] == ""


@pytest.mark.asyncio
async def test_patch_agent_chat_scoped_fields_conflicts():
    """The never-desync rule: agent-pinned chats refuse chat-scoped model/
    effort writes (the UI routes them at the agent instead)."""
    from backend.db.database import create_agent as db_create_agent
    import uuid

    cid = await create_conversation("pinned", chat_type="agent")
    record = await db_create_agent(
        {
            "id": uuid.uuid4().hex[:12],
            "workspace": "",
            "name": "a",
            "prompt": "p",
            "schedule_type": "interval",
            "schedule_spec": json.dumps({"minutes": 30}),
            "approval_policy": "sandbox-only",
            "enabled": 1,
            "memory_enabled": 1,
            "conversation_id": cid,
        }
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.patch(
            f"/api/conversations/{cid}", json={"model": "p1::zzz"}
        )
        assert r.status_code == 409
        assert "agent" in r.json()["detail"]
        # other fields still patch fine on an agent chat
        r = await c.patch(f"/api/conversations/{cid}", json={"title": "ok"})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_write_through_endpoint_updates_agent():
    from backend.db.database import create_agent as db_create_agent

    import uuid

    cid = await create_conversation("pinned2", chat_type="agent")
    aid = uuid.uuid4().hex[:12]
    await db_create_agent(
        {
            "id": aid,
            "workspace": "",
            "name": "a2",
            "prompt": "p",
            "schedule_type": "interval",
            "schedule_spec": json.dumps({"minutes": 30}),
            "approval_policy": "sandbox-only",
            "model": "",
            "effort": "",
            "enabled": 1,
            "memory_enabled": 1,
            "conversation_id": cid,
        }
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.patch(
            f"/api/agents/{aid}/model-effort",
            json={"model": "p2::night-model", "effort": "high"},
        )
        assert r.status_code == 200
        assert r.json()["model"] == "p2::night-model"
        assert r.json()["effort"] == "high"


@pytest.mark.asyncio
async def test_write_through_endpoint_404_unknown_agent():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.patch("/api/agents/zzz/model-effort", json={"model": "m", "effort": ""})
        assert r.status_code == 404


# ------------------------------------------------------------------- loop pass-through


class _FakeStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


@pytest.mark.asyncio
async def test_turn_uses_conversation_model_and_effort(monkeypatch, tmp_path):
    """#51/#76 end to end at loop level: overrides given to run_agent reach
    model_client.chat, and effort=None stays None (explicit no-param)."""
    from backend.db.database import create_conversation as cc

    seen: list[dict] = []

    async def fake_chat(messages, tools=None, stream=True, model="", effort=None):
        seen.append({"model": model, "effort": effort})
        return _FakeStream([{"type": "content", "text": "ok"}, {"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)

    cid = await cc("scoped")
    await update_conversation(cid, model="p1::chat-model", effort="high")
    events = []
    async for line in loop.run_agent(
        cid, "go", str(tmp_path), model_override="p1::chat-model", effort_override="high"
    ):
        events.append(json.loads(line))
    assert seen[0]["model"] == "p1::chat-model"
    assert seen[0]["effort"] == "high"

    await update_conversation(cid, model="", effort="")
    async for _ in loop.run_agent(
        cid, "go2", str(tmp_path), model_override="", effort_override=None
    ):
        pass
    assert seen[1]["model"] == ""
    assert seen[1]["effort"] is None
    assert events[-1]["type"] == "done"
