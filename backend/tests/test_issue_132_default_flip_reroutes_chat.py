"""Issue #132 red loop: flipping the sidebar default-model selector (a global
set_active_model) re-routes existing chats' turns to the NEW active provider
while the chat header keeps showing the chat's original model.

The user-visible symptom: the provider answers with its raw body ("model not
found"), surfaced as `Model API error {status}: {body}` from model_client.

Three slices, one per suspect link in the chain:
  1. end-to-end (loop-level): a chat created under global A, then the global
     flipped to provider B, still resolves its call to provider A's base.
  2. migration: _stamp_conversation_scopes must stamp provider-qualified ids.
  3. UI truth: the stored row value must name the provider it resolves to.
"""
import json

import httpx
import pytest

from backend.main import app
from backend.agent import config as config_mod
from backend.agent.config import load_config, set_active_model
from backend.db.database import create_conversation, get_conversation, update_conversation


def _write_config(active_provider: str):
    """Real config file, real load_config — the exact data the resolution
    chain sees, not a mock of it. Two providers with DISTINCT bases so a
    resolved api_base names its provider unambiguously. Keeps access_mode
    'full' per the conftest contract (the suite runs with the gate open)."""
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
            }
        ),
        encoding="utf-8",
    )


def _fake_provider(base: str, models: set[str]):
    """Minimal OpenAI-compatible router: 200 + one content chunk for a model
    it hosts, 404 + 'model not found' body for anything else — mirroring the
    real provider behavior that produces the user's symptom."""

    async def handler(request: httpx.Request) -> httpx.Response:
        # Catalog probe (providers.list_all_models): only the ids we host.
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [{"id": m} for m in sorted(models)]}
            )
        payload = json.loads(request.content)
        model = payload["model"]
        if model not in models:
            return httpx.Response(404, json={"error": {"message": f"model not found: {model}"}})
        if payload.get("stream"):
            chunk = json.dumps(
                {"id": "x", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"},
                              "finish_reason": None}]}
            )
            final = json.dumps(
                {"id": "x", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            )
            sse = f"data: {chunk}\n\ndata: {final}\n\ndata: [DONE]\n\n"
            return httpx.Response(
                200, text=sse, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={
                "id": "x", "object": "chat.completion", "created": 0, "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    return httpx.MockTransport(handler)


# The bases the loop must NOT cross for a chat pinned to the other provider.
@pytest.fixture
def route_providers(monkeypatch):
    """Patch the transports model_client uses so calls hit fake providers on
    the real resolution path — everything above the socket is untouched."""
    import backend.agent.model_client as mc

    transports = {
        "http://groq.test": _fake_provider("http://groq.test", {"llama-3.3-70b"}),
        "http://openai.test": _fake_provider("http://openai.test", {"gpt-5.2"}),
    }

    def _dispatch(request: httpx.Request) -> httpx.Response:
        inner = transports.get(f"http://{request.url.host}")
        if inner is None:
            return httpx.Response(404, json={"error": {"message": "no fake provider"}})
        return inner.handler(request)

    class RoutedClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            # Route every client that does not bring its own transport
            # (model_client builds bare AsyncClients); explicitly-passed
            # transports — the ASGI test client — stay untouched.
            if "transport" not in kw:
                kw["transport"] = httpx.MockTransport(_dispatch)
            super().__init__(*a, **kw)

    monkeypatch.setattr(mc.httpx, "AsyncClient", RoutedClient)
    return transports


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


async def _post_turn(conversation_id: int, message: str):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with client.stream(
            "POST",
            f"/api/agent/{conversation_id}",
            json={"message": message, "workspace": "."},
        ) as r:
            assert r.status_code == 200, await r.aread()
            events = []
            async for line in r.aiter_lines():
                if line.strip():
                    events.append(json.loads(line))
            return events


# --------------------------------------------------------------- slice 1: e2e


@pytest.mark.asyncio
async def test_default_flip_does_not_reroute_existing_chat(route_providers):
    """THE user scenario: chat created under groq default, sidebar default
    flipped to openai, next turn must still run on groq."""
    _write_config("groq")
    cid = await create_conversation("legacy chat")  # stamps the then-global
    conv = await get_conversation(cid)
    # #132 fix shape: the stored scope is COMPLETE (provider::model) — the
    # routing provider is decided at write time, not at call time.
    assert conv["model"] == "groq::llama-3.3-70b", conv["model"]

    # User changes the sidebar default model -> active_provider flips.
    set_active_model("openai", "gpt-5.2")
    assert load_config()["active_provider"] == "openai"

    events = await _post_turn(cid, "hello")
    # The turn must NOT be a model_api_error carrying the foreign provider's
    # "model not found" body — that is the user's exact symptom.
    errors = [e for e in events if e.get("type") == "error"]
    assert not errors, f"turn failed after default flip: {errors}"
    assert any(
        e.get("type") == "model_call" and e.get("provider") == "groq" for e in events
    ), [e.get("type") for e in events]


# ------------------------------------------------- repair pass for legacy rows


@pytest.mark.asyncio
async def test_repair_qualifies_existing_bare_rows(route_providers):
    """Rows written before the fix carry a bare id (routing lost). One-time
    startup repair: catalog-match the id across configured providers;
    unambiguous match wins; several hosts -> the then-ACTIVE provider; no
    match -> the active provider too; '' rows (deliberate Default) and
    already-qualified rows stay untouched. Covers conversations AND agents
    (scheduler fires resolve through the same bare branch)."""
    _write_config("groq")
    import uuid

    import backend.db.database as db_mod
    from backend.db.database import create_agent as db_create_agent
    from backend.db.database import update_conversation

    legacy = await create_conversation("legacy")
    # Seed PRE-FIX storage directly: update_conversation now qualifies bare
    # writes (#132), so the legacy shape only exists via raw SQL.
    db = await db_mod.get_db()
    try:
        await db.execute(
            "UPDATE conversations SET model = 'llama-3.3-70b' WHERE id = ?", (legacy,)
        )
        await db.commit()
    finally:
        await db.close()
    deliberate = await create_conversation("deliberate")
    await update_conversation(deliberate, model="", effort="")  # '' = Default
    already = await create_conversation("already")
    await update_conversation(already, model="openai::gpt-5.2")  # qualified

    agent_conv = await create_conversation("agent-chat", chat_type="agent")
    agent = await db_create_agent(
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
            "model": "llama-3.3-70b",  # bare, pre-fix
            "conversation_id": agent_conv,
        }
    )

    set_active_model("openai", "gpt-5.2")  # user flips the default

    await db_mod.repair_bare_model_scopes()

    # Catalog match wins over the (now-openai) active provider.
    assert (await get_conversation(legacy))["model"] == "groq::llama-3.3-70b"
    assert (await get_conversation(deliberate))["model"] == ""  # untouched
    assert (await get_conversation(already))["model"] == "openai::gpt-5.2"
    # Look the agent up by the id we created — not list_agents()[0], which
    # couples the assert to global suite ordering.
    agent_row = await db_mod.get_agent(agent["id"])
    assert agent_row["model"] == "groq::llama-3.3-70b"

    # Idempotent: a second pass rewrites nothing (qualified values stay).
    await db_mod.repair_bare_model_scopes()
    assert (await get_conversation(legacy))["model"] == "groq::llama-3.3-70b"
    assert (await db_mod.get_agent(agent_row["id"]))["model"] == "groq::llama-3.3-70b"


@pytest.mark.asyncio
async def test_repair_prefers_active_provider_on_ambiguity(route_providers):
    """The same id hosted by two providers resolves to the ACTIVE one at
    repair time (the global default the user last chose)."""
    _write_config("groq")
    from backend.db.database import update_conversation

    cid = await create_conversation("both-host-it")
    # Pre-fix bare storage via raw SQL (update_conversation qualifies now).
    import backend.db.database as db_mod

    db = await db_mod.get_db()
    try:
        await db.execute("UPDATE conversations SET model = 'shared-model' WHERE id = ?", (cid,))
        await db.commit()
    finally:
        await db.close()
    set_active_model("openai", "gpt-5.2")

    await db_mod.repair_bare_model_scopes()
    assert (await get_conversation(cid))["model"] == "openai::shared-model"


# ----------------------------------------------------------- slice 2: migration


@pytest.mark.asyncio
async def test_stamp_migration_qualifies_bare_global(monkeypatch):
    """#132 regression net for the STAMP itself: the #51/#76 upgrade stamp
    must write the global in provider-qualified form. Production
    load_config() serves a BARE cfg['model'] (derived from
    providers[active].model) — exactly the shape that stamped the buggy
    rows — so the stamp sees a bare id and must qualify it (the neighbor
    test_per_chat_scoping stamp test patches an already-qualified global,
    which passes through untested)."""
    import backend.db.database as db_mod

    _write_config("groq")
    db_mod._stamp_scope_done = False
    cid = await create_conversation("pre-stamp")
    # Simulate the pre-upgrade state: a row with blank scope columns.
    await update_conversation(cid, model="", effort="")

    db = await db_mod.get_db()
    try:
        await db_mod._stamp_conversation_scopes(db)
    finally:
        await db.close()

    row = await get_conversation(cid)
    assert row["model"] == "groq::llama-3.3-70b", (
        f"stamp wrote {row['model']!r}: routing provider is lost"
    )


# --------------------------------------------------------------- slice 3: UI truth


@pytest.mark.asyncio
async def test_pinned_row_value_names_its_provider(route_providers):
    """Pinning through the chat header picker stores provider::model — the
    row itself must be self-describing (the picker renders from it)."""
    _write_config("groq")
    cid = await create_conversation("pinned")
    await update_conversation(cid, model="groq::llama-3.3-70b")
    set_active_model("openai", "gpt-5.2")
    events = await _post_turn(cid, "hello")
    errors = [e for e in events if e.get("type") == "error"]
    assert not errors, f"qualified pin re-routed anyway: {errors}"
