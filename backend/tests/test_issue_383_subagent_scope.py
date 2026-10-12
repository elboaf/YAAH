"""Issue #383: the per-chat sub-agent model scope.

A third scope joins the family (#51/#76 model, #132 complete-or-empty):
the chat's sub-agent pin (complete "provider::model" or '' = sub-agent:
auto) stored on conversations/agents, written through the same pickers'
endpoints, repaired like model itself, and resolved per turn for
spawn_batch. These tests cover the API surface and the DB repair; the
resolver/batch precedence lives in test_subagent_provider.py.
"""
import json
import uuid

import httpx
import pytest

from backend.main import app
from backend.agent import config as config_mod
from backend.agent.config import set_active_model
from backend.db.database import (
    create_agent,
    create_conversation,
    get_agent,
    get_agent_for_conversation,
    get_conversation,
    update_conversation,
)


def _write_config(active_provider: str, subagent_provider: str = ""):
    """Real config file, real load_config — two providers with distinct
    bases and catalogs, access_mode 'full' per the conftest contract."""
    config_mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_mod.CONFIG_PATH.write_text(
        json.dumps(
            {
                "access_mode": "full",
                "providers": {
                    "groq": {
                        "api_base": "http://groq.test",
                        "api_key": "k-groq",
                        "model": "llama-3.3-70b",
                    },
                    "openai": {
                        "api_base": "http://openai.test",
                        "api_key": "k-openai",
                        "model": "gpt-5.2",
                    },
                },
                "active_provider": active_provider,
                "subagent_provider": subagent_provider,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _fresh_config():
    yield
    if config_mod.CONFIG_PATH.exists():
        config_mod.CONFIG_PATH.unlink()
    # Restore the conftest contract: the suite-wide config runs with the
    # access-mode gate open; later tests must not find it deleted/blank.
    config_mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_mod.CONFIG_PATH.write_text(
        json.dumps({"access_mode": "full"}), encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _reset_one_time_flags():
    """The stamp/repair passes run once per PROCESS by design; tests need a
    fresh pass each."""
    import backend.db.database as db_mod

    db_mod._stamp_scope_done = False
    db_mod._repair_scope_done = False
    yield
    db_mod._stamp_scope_done = False
    db_mod._repair_scope_done = False


@pytest.fixture
def catalogs(monkeypatch):
    """Fake /models catalogs (groq hosts llama-3.3-70b, openai gpt-5.2) so
    the bare-scope repair resolves without network."""
    from backend.agent.providers import list_all_models

    async def _fake(cfg_providers):
        return {
            "providers": {
                "groq": {"models": ["llama-3.3-70b"], "error": None},
                "openai": {"models": ["gpt-5.2"], "error": None},
            }
        }

    monkeypatch.setattr(list_all_models, "__wrapped__", _fake, raising=False)
    import backend.agent.providers as providers_mod

    monkeypatch.setattr(providers_mod, "list_all_models", _fake)


async def _api():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


# ----------------------------------------------------------- conversation PATCH


@pytest.mark.asyncio
async def test_conversation_patch_writes_subagent_scope():
    _write_config("groq")
    cid = await create_conversation("scoped")
    async with await _api() as client:
        r = await client.patch(
            f"/api/conversations/{cid}", json={"subagent_model": "openai::gpt-5.2"}
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
    assert (await get_conversation(cid))["subagent_model"] == "openai::gpt-5.2"


@pytest.mark.asyncio
async def test_conversation_patch_empty_clears_subagent_scope():
    _write_config("groq")
    cid = await create_conversation("scoped", subagent_model="openai::gpt-5.2")
    async with await _api() as client:
        r = await client.patch(f"/api/conversations/{cid}", json={"subagent_model": ""})
        assert r.status_code == 200
    assert (await get_conversation(cid))["subagent_model"] == ""


@pytest.mark.asyncio
async def test_conversation_patch_qualifies_bare_subagent_scope():
    """#132 rule: a bare id is qualified against the active provider at
    write — the stored row must be self-describing."""
    _write_config("groq")
    cid = await create_conversation("scoped")
    async with await _api() as client:
        r = await client.patch(
            f"/api/conversations/{cid}", json={"subagent_model": "gpt-5.2"}
        )
        assert r.status_code == 200
    assert (await get_conversation(cid))["subagent_model"] == "groq::gpt-5.2"


@pytest.mark.asyncio
async def test_agent_chat_patch_subagent_scope_is_409():
    """#51/#76 contract extends to the third picker: the pinned chat of a
    scheduled agent writes through the agent endpoint, never the chat's."""
    _write_config("groq")
    conv = await create_conversation("agent-chat", chat_type="agent")
    await create_agent(
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
            "conversation_id": conv,
        }
    )
    async with await _api() as client:
        r = await client.patch(
            f"/api/conversations/{conv}", json={"subagent_model": "openai::gpt-5.2"}
        )
        assert r.status_code == 409


# ----------------------------------------------------------- agent write-through


@pytest.mark.asyncio
async def test_agent_model_effort_endpoint_writes_subagent_scope():
    _write_config("groq")
    conv = await create_conversation("agent-chat", chat_type="agent")
    agent = await create_agent(
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
            "conversation_id": conv,
        }
    )
    async with await _api() as client:
        r = await client.patch(
            f"/api/agents/{agent['id']}/model-effort",
            json={"model": "", "effort": "", "subagent_model": "openai::gpt-5.2"},
        )
        assert r.status_code == 200
    import backend.db.database as db_mod

    row = await db_mod.get_agent(agent["id"])
    assert row["subagent_model"] == "openai::gpt-5.2"


@pytest.mark.asyncio
async def test_agent_view_exposes_subagent_model():
    _write_config("groq")
    conv = await create_conversation("agent-chat", chat_type="agent")
    agent = await create_agent(
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
            "subagent_model": "openai::gpt-5.2",
            "conversation_id": conv,
        }
    )
    async with await _api() as client:
        r = await client.get("/api/agents")
        assert r.status_code == 200
        views = r.json()["agents"]
        view = next(a for a in views if a["id"] == agent["id"])
        assert view["subagent_model"] == "openai::gpt-5.2"


# ----------------------------------------------------------- turn resolution


@pytest.mark.asyncio
async def test_resolve_turn_scope_returns_chat_subagent_scope():
    _write_config("groq")
    from backend.main import _resolve_turn_scope

    cid = await create_conversation("scoped", subagent_model="openai::gpt-5.2")
    conv = await get_conversation(cid)
    model, effort, sub_scope = _resolve_turn_scope(conv, None)
    assert sub_scope == "openai::gpt-5.2"
    # The primary scope is the stamped global default — untouched by the
    # sub-agent pin (the two scopes are independent).
    assert model == "groq::llama-3.3-70b"


@pytest.mark.asyncio
async def test_resolve_turn_scope_agent_chat_reads_agent_row():
    _write_config("groq")
    from backend.main import _resolve_turn_scope

    conv_id = await create_conversation("agent-chat", chat_type="agent")
    await create_agent(
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
            "subagent_model": "llama-3.3-70b",  # bare: qualify at resolve
            "conversation_id": conv_id,
        }
    )
    import backend.db.database as db_mod

    conv = await get_conversation(conv_id)
    agent = await db_mod.get_agent_for_conversation(conv_id)
    _, _, sub_scope = _resolve_turn_scope(conv, agent)
    assert sub_scope == "groq::llama-3.3-70b"


# ----------------------------------------------------------- repair pass


@pytest.mark.asyncio
async def test_repair_qualifies_bare_subagent_scope(catalogs):
    """#383: the boot repair pass covers the new columns in BOTH tables —
    a bare subagent scope routes at the ambient provider otherwise,
    silently defeating the pin."""
    _write_config("groq")
    import backend.db.database as db_mod

    cid = await create_conversation("bare-sub")
    db = await db_mod.get_db()
    try:
        await db.execute(
            "UPDATE conversations SET subagent_model = 'llama-3.3-70b' WHERE id = ?",
            (cid,),
        )
        await db.commit()
    finally:
        await db.close()
    conv_id_agent = await create_conversation("agent-chat", chat_type="agent")
    agent = await create_agent(
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
            "conversation_id": conv_id_agent,
        }
    )
    db = await db_mod.get_db()
    try:
        await db.execute(
            "UPDATE agents SET subagent_model = 'gpt-5.2' WHERE id = ?", (agent["id"],)
        )
        await db.commit()
    finally:
        await db.close()

    set_active_model("openai", "gpt-5.2")
    await db_mod.repair_bare_model_scopes()

    # Unique catalog match names its provider (not the active one).
    assert (await get_conversation(cid))["subagent_model"] == "groq::llama-3.3-70b"
    row = await db_mod.get_agent(agent["id"])
    assert row["subagent_model"] == "openai::gpt-5.2"

    # Idempotent.
    await db_mod.repair_bare_model_scopes()
    assert (await get_conversation(cid))["subagent_model"] == "groq::llama-3.3-70b"


# ----------------------------------------------------------- config surface


@pytest.mark.asyncio
async def test_config_get_returns_subagent_provider():
    _write_config("groq", subagent_provider="openai::gpt-5.2")
    async with await _api() as client:
        r = await client.get("/api/config")
        assert r.status_code == 200
        assert r.json()["subagent_provider"] == "openai::gpt-5.2"
