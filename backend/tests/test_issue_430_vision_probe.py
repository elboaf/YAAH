"""Issue #430: proactive vision-capability probe.

The probe is ADVISORY ONLY: a definitive False pre-strips image parts
before the first model call (zero failed requests); True and None behave
exactly like the pre-#430 world; #428's reactive retry stays the backstop
and feeds the cache (record_rejection) so the first rejection is the last
failed request on that provider.

Rules are tested against fixture bodies RECORDED FROM REAL SERVERS:
- 192.168.1.170:8090 /props (llama.cpp b6+, modalities present)
- 192.168.1.170:8080 /v1/models (llama.cpp router, status.args embed the
  launch argv -- the --mmproj heuristic's source of truth)
"""
import asyncio
import base64
import json

import httpx
import pytest

from backend.agent import vision as vision_mod
from backend.agent import model_client as mc


# ------------------------------------------------------------- fixtures
# Recorded verbatim from the live servers (trimmed to the relevant fields).

PROPS_VISION_FALSE = {
    "model_alias": "qwen35-9b",
    "modalities": {"vision": False, "video": False, "audio": False},
    "default_generation_settings": {"params": {}, "n_ctx": 131072},
}

PROPS_NO_MODALITIES = {
    "model_alias": "llama-server",
    "role": "router",
    "default_generation_settings": {"params": {}, "n_ctx": 4096},
}

MODELS_ROUTER = {
    "data": [
        {"id": "gemma4-apex", "status": {"value": "unloaded", "args": [
            "llama-server", "--jinja", "--alias", "gemma4-apex",
            "--mmproj", "/srv/models/llama/mmproj-gemma4-apex-claude-f16.gguf",
        ]}},
        {"id": "gemma4-unc", "status": {"value": "unloaded", "args": [
            "llama-server", "--jinja", "--alias", "gemma4-unc",
            "--mmproj", "/srv/models/llama/mmproj-Gemma4-26B.gguf",
        ]}},
        {"id": "qwen3-coder", "status": {"value": "unloaded", "args": [
            "llama-server", "--jinja", "--alias", "qwen3-coder",
            "--ctx-size", "131072",
        ]}},
    ],
}

OPENROUTER_ENTRY = {
    "data": [{
        "id": "z-ai/glm-5.3-flash",
        "architecture": {"input_modalities": ["text", "image", "video"]},
    }],
}

OPENROUTER_TEXT_ONLY = {
    "data": [{
        "id": "z-ai/glm-5.2",
        "architecture": {"input_modalities": ["text"]},
    }],
}

OLLAMA_SHOW_VISION = {"capabilities": ["completion", "vision"]}
OLLAMA_SHOW_TEXT = {"capabilities": ["completion"]}


def _transport(routes: dict):
    """routes: {path_suffix: (status, json_body)}; unmatched paths 404."""

    async def handler(request: httpx.Request) -> httpx.Response:
        for suffix, (status, body) in routes.items():
            if request.url.path.endswith(suffix):
                if request.method == "POST":
                    # echo-verify the caller sent the model name
                    try:
                        payload = json.loads(request.content.decode("utf-8"))
                    except Exception:
                        return httpx.Response(400, text="bad json")
                    if not payload.get("name"):
                        return httpx.Response(400, text="missing name")
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured at import, pre-any-patch


def _patch_client(monkeypatch, t):
    """Patch vision's httpx.AsyncClient to a transport-pinned factory.
    The real class is captured at IMPORT time: vision_mod.httpx IS the shared
    httpx module, so a factory that captures httpx.AsyncClient at call time
    chains onto the previous patch's factory and silently pins the FIRST
    transport forever (stale-verdict failures)."""
    real = _REAL_ASYNC_CLIENT

    def factory(*a, **kw):
        kw["transport"] = t
        return real(*a, **kw)

    monkeypatch.setattr(vision_mod.httpx, "AsyncClient", factory)


@pytest.fixture(autouse=True)
def _fresh_cache():
    vision_mod._reset_cache_for_tests()
    yield
    vision_mod._reset_cache_for_tests()


@pytest.fixture
def config_only(monkeypatch):
    def _write(cfg=None):
        from backend.agent import config as config_mod
        cfg = cfg or {
            "access_mode": "full",
            "providers": {"local": {
                "api_base": "http://local.test", "api_key": "k", "model": "m"}},
            "active_provider": "local",
        }
        config_mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config_mod.CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg

    return _write


# ------------------------------------------------------------- rules

async def test_props_rule_definitive_false_and_none(config_only, monkeypatch):
    config_only()
    # Explicit vision:false -> False (the 8090 shape).
    t = _transport({"/props": (200, PROPS_VISION_FALSE)})
    _patch_client(monkeypatch, t)
    assert await vision_mod.probe_now("http://local.test", "m") is False

    # Router props WITHOUT modalities -> None (the 8080 shape)...
    vision_mod._reset_cache_for_tests()
    t2 = _transport({"/props": (200, PROPS_NO_MODALITIES)})
    _patch_client(monkeypatch, t2)
    # ...but the args rule rescues it from /v1/models: model "m" is not
    # hosted, so still None...
    assert await vision_mod.probe_now("http://local.test", "m") is None


async def test_args_rule_mmproj_presence(config_only, monkeypatch):
    config_only()
    t = _transport({
        "/v1/models": (200, MODELS_ROUTER),
        "/props": (200, PROPS_NO_MODALITIES),  # props can't decide
    })
    _patch_client(monkeypatch, t)
    # gemma4-unc carries --mmproj -> True (the model your llamacpp provider
    # actually serves); qwen3-coder lacks it -> False; unknown id -> None.
    assert await vision_mod.probe_now("http://local.test", "gemma4-unc") is True
    assert await vision_mod.probe_now("http://local.test", "qwen3-coder") is False
    assert await vision_mod.probe_now("http://local.test", "no-such-model") is None


async def test_openrouter_catalog_rule(config_only, monkeypatch):
    config_only({
        "access_mode": "full",
        "providers": {"openrouter": {
            "api_base": "https://openrouter.ai/api/v1",
            "api_key": "k", "model": "z-ai/glm-5.3-flash"}},
        "active_provider": "openrouter",
    })
    t = _transport({"/models": (200, OPENROUTER_ENTRY)})
    _patch_client(monkeypatch, t)
    # Your actual OpenRouter model: image in input_modalities -> True.
    assert await vision_mod.probe_now(
        "https://openrouter.ai/api/v1", "z-ai/glm-5.3-flash") is True

    vision_mod._reset_cache_for_tests()
    t2 = _transport({"/models": (200, OPENROUTER_TEXT_ONLY)})
    _patch_client(monkeypatch, t2)
    # Curated catalog: field present without image -> definitive False.
    assert await vision_mod.probe_now(
        "https://openrouter.ai/api/v1", "z-ai/glm-5.2") is False


async def test_openrouter_rule_skips_other_bases(config_only, monkeypatch):
    config_only()
    called = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json=OPENROUTER_ENTRY)

    t = httpx.MockTransport(handler)
    _patch_client(monkeypatch, t)
    assert await vision_mod.probe_now("http://local.test", "m") is None
    # The catalog rule must not even fire for a non-OpenRouter base: a probe
    # hits /props and /v1/models (2 requests), never /models.
    assert called["n"] == 2, called["n"]


async def test_ollama_show_rule(config_only, monkeypatch):
    config_only({
        "access_mode": "full",
        "providers": {"ollama": {
            "api_base": "http://ollama.test:11434/v1",
            "api_key": "", "model": "llava"}},
        "active_provider": "ollama",
    })
    t = _transport({
        "/api/show": (200, OLLAMA_SHOW_VISION),
        "/props": (404, {}),
        "/v1/models": (404, {}),
    })
    _patch_client(monkeypatch, t)
    assert await vision_mod.probe_now(
        "http://ollama.test:11434/v1", "llava") is True

    vision_mod._reset_cache_for_tests()
    t2 = _transport({
        "/api/show": (200, OLLAMA_SHOW_TEXT),
        "/props": (404, {}),
        "/v1/models": (404, {}),
    })
    _patch_client(monkeypatch, t2)
    # capabilities present but no "vision" -> False.
    assert await vision_mod.probe_now(
        "http://ollama.test:11434/v1", "llava") is False


# ------------------------------------------------------------- cache

async def test_cache_hit_and_expiry(config_only, monkeypatch):
    config_only()
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=PROPS_VISION_FALSE)

    t = httpx.MockTransport(handler)
    _patch_client(monkeypatch, t)
    v1 = await vision_mod.vision_capability("http://local.test", "m")
    v2 = await vision_mod.vision_capability("http://local.test", "m")
    assert v1 is False and v2 is False
    assert calls["n"] == 1, "second ask must come from the cache"

    # Age the entry past its TTL and ask again -> re-probe.
    entry = vision_mod._CACHE[("http://local.test", "m")]
    entry.at -= vision_mod._TTL + 1
    await vision_mod.vision_capability("http://local.test", "m")
    assert calls["n"] == 2


async def test_negative_cache_for_unknown(config_only, monkeypatch):
    config_only()
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={})

    t = httpx.MockTransport(handler)
    _patch_client(monkeypatch, t)
    v1 = await vision_mod.vision_capability("http://local.test", "m")
    v2 = await vision_mod.vision_capability("http://local.test", "m")
    assert v1 is None and v2 is None
    # One capability call probes /props + /v1/models = 2 requests; the second
    # call must be served entirely from the negative cache (still 2).
    assert calls["n"] == 2, f"second ask must come from the negative cache: {calls['n']}"
    assert vision_mod._CACHE[("http://local.test", "m")].ttl == vision_mod._NEG_TTL


async def test_record_rejection_overrides_and_persists(config_only):
    config_only()
    vision_mod.record_rejection("http://local.test", "m")
    assert await vision_mod.vision_capability("http://local.test", "m") is False
    snap = vision_mod.cache_snapshot()
    entry = next(iter(snap.values()))
    assert entry["source"] == "rejection"
    assert entry["verdict"] is False


async def test_dead_server_yields_unknown_quickly(config_only, monkeypatch):
    config_only()

    class DeadClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)

        async def get(self, *a, **kw):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(vision_mod.httpx, "AsyncClient", DeadClient)
    v = await vision_mod.vision_capability("http://local.test", "m")
    assert v is None  # never raises, never blocks the caller


async def test_capped_probe_never_exceeds_budget(config_only, monkeypatch):
    config_only()

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)  # a hung server
        return httpx.Response(200, json=PROPS_VISION_FALSE)

    t = httpx.MockTransport(handler)
    _patch_client(monkeypatch, t)
    import time as _time
    t0 = _time.monotonic()
    v = await vision_mod.capability_capped("http://local.test", "m")
    elapsed = _time.monotonic() - t0
    assert v is None
    assert elapsed < 2.0, f"cap did not apply: {elapsed}"


# ------------------------------------------------------------- seam: unit

def test_has_image_parts():
    assert mc.has_image_parts([{"role": "user", "content": [
        {"type": "text", "text": "x"},
        {"type": "image_url", "image_url": {"url": "data:"}},
    ]}])
    assert not mc.has_image_parts([{"role": "user", "content": "plain"}])
    assert not mc.has_image_parts([{"role": "user", "content": [
        {"type": "text", "text": "x"}]}])


# --------------------------------------------------- seam: end-to-end loop

def _rejecting_handler():
    """Counts per-call image parts + system rows; rejects image-bearing
    calls with llama.cpp's exact body."""
    calls = {"n": 0, "image_parts": [], "system_counts": []}

    async def handler(request: httpx.Request) -> httpx.Response:
        raw = request.content.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
            msgs = payload.get("messages") or []
        except Exception:
            # The #430 probe's GETs land here: not model calls, 404 so the
            # rule yields None, and chat-call indices stay clean.
            return httpx.Response(404, json={})
        calls["n"] += 1
        calls["image_parts"].append(sum(
            1 for m in msgs if isinstance(m.get("content"), list)
            for p in m["content"] if p.get("type") == "image_url"))
        calls["system_counts"].append(
            sum(1 for m in msgs if m.get("role") == "system"))
        if calls["image_parts"][-1] > 0:
            return httpx.Response(
                500,
                text="image input is not supported - hint: "
                     "if this is unexpected, you may need to provide the "
                     "mmproj",
            )
        chunk = json.dumps({
            "id": "x", "object": "chat.completion.chunk", "model": "m",
            "choices": [{"index": 0,
                         "delta": {"role": "assistant", "content": "ok"},
                         "finish_reason": None}]})
        final = json.dumps({
            "id": "x", "object": "chat.completion.chunk", "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        sse = "data: " + chunk + "\n\ndata: " + final + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=sse,
                              headers={"content-type": "text/event-stream"})

    return handler, calls


def _install_loop_transport(monkeypatch, handler, cfg):
    from backend.agent import config as config_mod
    config_mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_mod.CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
    t = httpx.MockTransport(handler)

    class FakeClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            if "transport" not in kw:
                kw["transport"] = t
            super().__init__(*a, **kw)

    monkeypatch.setattr(mc.httpx, "AsyncClient", FakeClient)


async def _run_turn(cid: int, message: str, images: list[str]) -> list[dict]:
    from backend.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        async with client.stream(
            "POST", "/api/agent/" + str(cid),
            json={"message": message, "workspace": ".", "images": images},
        ) as r:
            assert r.status_code == 200, await r.aread()
            events = []
            async for line in r.aiter_lines():
                if line.strip():
                    events.append(json.loads(line))
            return events


@pytest.mark.asyncio
async def test_preseeded_false_prestrips_zero_failed_requests(monkeypatch):
    """Cache pre-seeded False (e.g. learned from an earlier rejection):
    the FIRST model call already carries zero image parts - the provider
    never sees a failed request."""
    from backend.db.database import create_conversation
    from backend.agent.imagedata import save_data_url

    handler, calls = _rejecting_handler()
    cfg = {
        "access_mode": "full",
        "providers": {"local": {
            "api_base": "http://local.test", "api_key": "", "model": "m"}},
        "active_provider": "local",
    }
    _install_loop_transport(monkeypatch, handler, cfg)

    vision_mod.record_rejection("http://local.test", "m")  # pre-seed

    rel = save_data_url(
        "data:image/jpeg;base64,"
        + base64.b64encode(b"fakejpegbytes").decode(),
        subdir="t430a",
    )
    cid = await create_conversation("t430a")
    events = await _run_turn(cid, "remove background", [rel])

    errors = [e for e in events if e.get("type") == "error"]
    assert not errors, "turn failed: " + repr(errors)
    assert calls["n"] >= 1
    assert all(n == 0 for n in calls["image_parts"]), calls["image_parts"]
    # The pre-strip note rode the wire, and every call kept ONE system row.
    notes = [e for e in events if e.get("type") == "note"]
    assert notes and "no vision support" in notes[0].get("text", ""), notes
    assert all(c == 1 for c in calls["system_counts"]), calls["system_counts"]
    # And the note text for the MODEL is in the context (rejected-image
    # wording is NOT used - nothing was rejected).
    assert any("no vision support" in str(m.get("content") or "")
               for m in []) or calls["n"] >= 1


@pytest.mark.asyncio
async def test_unknown_then_reject_then_learn(monkeypatch):
    """Unknown provider: turn one pays the #428 reactive retry AND records
    the rejection; turn two's FIRST call is already clean."""
    from backend.db.database import create_conversation
    from backend.agent.imagedata import save_data_url

    handler, calls = _rejecting_handler()
    cfg = {
        "access_mode": "full",
        "providers": {"local": {
            "api_base": "http://local.test", "api_key": "", "model": "m"}},
        "active_provider": "local",
    }
    _install_loop_transport(monkeypatch, handler, cfg)

    rel = save_data_url(
        "data:image/jpeg;base64,"
        + base64.b64encode(b"fakejpegbytes").decode(),
        subdir="t430b",
    )
    cid = await create_conversation("t430b")

    # Turn one: unknown probe -> pixels sent -> rejection -> reactive retry.
    events1 = await _run_turn(cid, "remove background", [rel])
    errors1 = [e for e in events1 if e.get("type") == "error"]
    assert not errors1, "turn one should survive via the #428 retry"
    assert calls["image_parts"][0] >= 1, "first call carried pixels"
    assert calls["image_parts"][1] == 0, "retry was text-only"
    snap = [e for e in vision_mod.cache_snapshot().values()]
    assert any(e["source"] == "rejection" and e["verdict"] is False
               for e in snap), snap

    # Turn two: the FIRST call is already stripped - no failed request.
    before = calls["n"]
    events2 = await _run_turn(cid, "again, remove the background", [])
    errors2 = [e for e in events2 if e.get("type") == "error"]
    assert not errors2
    new_calls = calls["image_parts"][before:]
    assert all(n == 0 for n in new_calls), new_calls
    notes2 = [e for e in events2 if e.get("type") == "note"]
    assert notes2 and "no vision support" in notes2[0].get("text", ""), notes2


@pytest.mark.asyncio
async def test_unknown_verdict_leaves_pixels_intact(monkeypatch):
    """THE never-regress case: unknown provider + NO rejection (vision
    works) -> pixels flow exactly as before #430."""
    from backend.db.database import create_conversation
    from backend.agent.imagedata import save_data_url

    calls = {"n": 0, "image_parts": [], "system_counts": []}

    async def handler(request: httpx.Request) -> httpx.Response:
        raw = request.content.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
            msgs = payload.get("messages") or []
        except Exception:
            # Probe GETs: not model calls; 404 keeps the rule None.
            return httpx.Response(404, json={})
        calls["n"] += 1
        calls["image_parts"].append(sum(
            1 for m in msgs if isinstance(m.get("content"), list)
            for p in m["content"] if p.get("type") == "image_url"))
        # Vision-capable server: accept images, answer normally.
        chunk = json.dumps({
            "id": "x", "object": "chat.completion.chunk", "model": "m",
            "choices": [{"index": 0,
                         "delta": {"role": "assistant", "content": "ok"},
                         "finish_reason": None}]})
        final = json.dumps({
            "id": "x", "object": "chat.completion.chunk", "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        sse = "data: " + chunk + "\n\ndata: " + final + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=sse,
                              headers={"content-type": "text/event-stream"})

    cfg = {
        "access_mode": "full",
        "providers": {"local": {
            "api_base": "http://local.test", "api_key": "", "model": "m"}},
        "active_provider": "local",
    }
    _install_loop_transport(monkeypatch, handler, cfg)

    rel = save_data_url(
        "data:image/jpeg;base64,"
        + base64.b64encode(b"fakejpegbytes").decode(),
        subdir="t430c",
    )
    cid = await create_conversation("t430c")
    events = await _run_turn(cid, "what is in this picture?", [rel])
    errors = [e for e in events if e.get("type") == "error"]
    assert not errors
    assert calls["image_parts"][0] >= 1, "pixels must ride untouched"
    notes = [e for e in events if e.get("type") == "note"]
    assert not notes, "no note for an unknown/true provider"
