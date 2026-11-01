"""Issue #428: a provider that rejects image input must not kill the turn.

llama.cpp-class servers without a mmproj validate the request BEFORE prefill
and reject the whole call ("image input is not supported - hint: ... provide
the mmproj"). The caller's auto-retry (Q23) is byte-identical, so it failed
the same way and the turn died - before the model ever read the #406
store-rel annotation that would let it wire generate_image reference_images.

Fix shape: classify the rejection in model_client, strip every image_url
part (text parts - with the store-rel annotations - survive), retry once
text-only, and tell both the model and the user what happened.

Slices:
  1. unit: strip_image_parts + the marker classifier (pure functions).
  2. model_client: both call paths raise ModelImageRejected on the marker
     body; generic errors still raise ModelError.
  3. end-to-end (loop-level): an attached image + rejecting provider ->
     the turn SURVIVES, one text-only retry carries no image parts, the
     note event is emitted, and the transcript keeps the image rel.
"""
import base64
import json

import httpx
import pytest

from backend.agent import model_client as mc


# --------------------------------------------------------- slice 1: unit

def _user_parts():
    return [
        {"type": "text", "text": "remove background"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        {"type": "text", "text": "[attached image, stored rel: 883/x.jpg - pass this path to generate_image reference_images]"},
    ]


def test_strip_keeps_text_parts_and_annotations():
    msgs = [
        {"role": "system", "content": "be good"},
        {"role": "user", "content": _user_parts()},
    ]
    out, n = mc.strip_image_parts(msgs)
    assert n == 1
    assert out[1]["role"] == "user"
    assert out[1]["content"] == (
        "remove background\n"
        "[attached image, stored rel: 883/x.jpg - pass this path to "
        "generate_image reference_images]"
    )
    # original untouched
    assert isinstance(msgs[1]["content"], list) and len(msgs[1]["content"]) == 3


def test_strip_passes_through_tool_calls_and_collapses_tool_parts():
    msgs = [
        {"role": "assistant", "content": "ok",
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1",
         "content": [{"type": "text", "text": "result"},
                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}]},
    ]
    out, n = mc.strip_image_parts(msgs)
    assert n == 1
    assert out[0]["tool_calls"][0]["id"] == "1"
    # a tool row's image part (view_image result) collapses to its text too
    assert out[1]["content"] == "result"
    assert out[1]["role"] == "tool"


def test_marker_classifier_matches_rejections_only():
    assert mc._looks_like_image_rejection(500, "image input is not supported - hint: ... provide the mmproj")
    assert mc._looks_like_image_rejection(400, "this model does not support images")
    assert not mc._looks_like_image_rejection(200, "image input is not supported")
    assert not mc._looks_like_image_rejection(500, "CUDA out of memory")
    assert not mc._looks_like_image_rejection(500, "model not found: x")


# ---------------------------------------------------- slice 2: model_client

def _config_only():
    return {
        "providers": {"local": {"api_base": "http://local.test", "api_key": "", "model": "m"}},
        "active_provider": "local",
        "api_base": "http://local.test",
        "api_key": "",
        "model": "m",
        "reasoning_effort": "",
    }


def _rejecting_provider(status=500,
                        body="image input is not supported - hint: if this is unexpected, you may need to provide the mmproj"):
    calls = {"n": 0, "image_parts": [], "system_counts": [], "notes": []}

    async def handler(request: httpx.Request) -> httpx.Response:
        # Count ONLY model calls. Since #430 the vision probe rides the
        # same patched httpx client (GET /props, /v1/models before the
        # first model call); counting those as calls shifted the e2e
        # indexes and broke "first call must carry the image" (caught
        # by the rc.29 release gate). Non-model requests are answered
        # without touching the counters; probe GETs 404, which keeps
        # every probe rule at None - the unknown-provider world this
        # suite was written for.
        if request.method != "POST" or not request.url.path.endswith(
            "/chat/completions"
        ):
            return httpx.Response(404, json={"error": "not found"})
        calls["n"] += 1
        raw = request.content.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
            msgs = payload.get("messages") or []
        except Exception:
            # Not model_client's request shape (e.g. the title probe) - no
            # image parts to count.
            msgs = []
        calls["system_counts"].append(
            sum(1 for m in msgs if m.get("role") == "system"))
        calls["notes"].append(any(
            "rejected image input" in str(m.get("content") or "")
            for m in msgs if m.get("role") == "system"))
        calls["image_parts"].append(
            sum(1 for m in msgs
                if isinstance(m.get("content"), list)
                for p in m["content"] if p.get("type") == "image_url")
        )
        if calls["image_parts"][-1] > 0:
            return httpx.Response(status, text=body)
        chunk = json.dumps({"id": "x", "object": "chat.completion.chunk", "model": "m",
                            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"},
                                         "finish_reason": None}]})
        final = json.dumps({"id": "x", "object": "chat.completion.chunk", "model": "m",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        sse = "data: " + chunk + "\n\ndata: " + final + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler), calls


@pytest.fixture
def rejecting_transport(monkeypatch):
    transport, calls = _rejecting_provider()
    monkeypatch.setattr(mc, "load_config", lambda: _config_only())

    class FakeClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            # Route only clients without their own transport - the ASGI
            # test client's transport must stay untouched.
            if "transport" not in kw:
                kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(mc.httpx, "AsyncClient", FakeClient)
    return calls


async def test_stream_path_classifies_image_rejection(rejecting_transport):
    msgs = [{"role": "user", "content": _user_parts()}]
    with pytest.raises(mc.ModelImageRejected):
        stream = await mc.chat(msgs, tools=None, stream=True)
        async for _ev in stream:
            pass
    assert rejecting_transport["n"] == 1


async def test_nonstream_path_classifies_image_rejection(rejecting_transport):
    msgs = [{"role": "user", "content": _user_parts()}]
    with pytest.raises(mc.ModelImageRejected):
        await mc.chat(msgs, tools=None, stream=False)
    assert rejecting_transport["n"] == 1


# ------------------------------------------------------- slice 3: end-to-end

async def _run_turn(cid: int, message: str, images: list[str]) -> list[dict]:
    from backend.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with client.stream(
            "POST",
            "/api/agent/" + str(cid),
            json={"message": message, "workspace": ".", "images": images},
        ) as r:
            assert r.status_code == 200, await r.aread()
            events = []
            async for line in r.aiter_lines():
                if line.strip():
                    events.append(json.loads(line))
            return events


@pytest.mark.asyncio
async def test_image_rejection_retries_textonly_and_survives(monkeypatch):
    """THE user scenario: attached image + a provider with no vision -> the
    turn must NOT die; the retry must carry zero image parts but keep the
    store-rel annotation text, the note must be emitted, and the persisted
    user row keeps its image rel."""
    from backend.agent import config as config_mod
    from backend.db.database import create_conversation, get_messages

    transport, calls = _rejecting_provider()
    config_mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_mod.CONFIG_PATH.write_text(json.dumps(
        {"access_mode": "full",
         "providers": {"local": {"api_base": "http://local.test", "api_key": "", "model": "m"}},
         "active_provider": "local"}
    ), encoding="utf-8")

    class FakeClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            # Route only clients without their own transport - the ASGI
            # test client's transport must stay untouched.
            if "transport" not in kw:
                kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(mc.httpx, "AsyncClient", FakeClient)

    from backend.agent.imagedata import save_data_url
    rel = save_data_url(
        "data:image/jpeg;base64," + base64.b64encode(b"fakejpegbytes").decode(),
        subdir="t428",
    )

    cid = await create_conversation("t428")
    events = await _run_turn(cid, "remove background", [rel])

    # The turn survived: no error event, at least two model calls, and the
    # retry sent ZERO image parts while call one sent one.
    errors = [e for e in events if e.get("type") == "error"]
    assert not errors, "turn failed: " + repr(errors)
    assert len(calls["image_parts"]) >= 2, calls
    assert calls["image_parts"][0] >= 1, "first call must carry the image"
    assert all(n == 0 for n in calls["image_parts"][1:]), calls

    notes = [e for e in events if e.get("type") == "note"]
    assert notes and "image" in notes[0].get("text", ""), notes

    # Strict-template contract: the retry context must carry exactly ONE
    # system message (a second system row 500s the llama.cpp qwen template),
    # with the note merged into it - and no note on the first call.
    assert all(c == 1 for c in calls["system_counts"]), calls["system_counts"]
    assert calls["notes"][0] is False, "first call must be note-free"
    assert all(calls["notes"][1:]), calls["notes"]

    # The persisted user row keeps the image rel (vision providers still
    # see the image on resume; the strip is live-context only).
    rows = await get_messages(cid)
    user_rows = [r for r in rows if r["role"] == "user"]
    assert user_rows and user_rows[0].get("images") == [rel], user_rows[0].get("images")


async def test_unrelated_500_is_not_image_rejection(monkeypatch):
    """A non-image 500 must stay a plain ModelError (no strip path)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="CUDA out of memory")

    cfg = _config_only()
    monkeypatch.setattr(mc, "load_config", lambda: cfg)
    transport2 = httpx.MockTransport(handler)

    class FakeClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = transport2
            super().__init__(*a, **kw)

    monkeypatch.setattr(mc.httpx, "AsyncClient", FakeClient)

    msgs = [{"role": "user", "content": "hello"}]
    with pytest.raises(mc.ModelError) as ei:
        stream = await mc.chat(msgs, tools=None, stream=True)
        async for _ev in stream:
            pass
    assert "CUDA" in str(ei.value)
    assert not isinstance(ei.value, mc.ModelImageRejected)
