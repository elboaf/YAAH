"""LoRA auto-apply matches the USER's typed words only (#439).

The live failure (chat 901): the user typed "put the man from image 2
walking across the sidewalk in image 1"; the model composed the tool-call
prompt itself ("...neon blue and black stylized sidewalk... cyberpunk
aesthetic...") describing the reference image's palette - and the
filename-derived tokens of ck-neon-retrowave-klein-9b ("neon" +
"retrowave") scored the 0.5 bar off that composed prose, auto-applying a
retrowave LoRA the user never asked for and wrecking the edit.

The contract now: the auto-matcher's ONLY corpus is the user's typed
words for the current turn, injected by the harness at the dispatch
funnel (the #303/#346 pattern). The model's composed prompt never
decides a match; absent user text means no content match (fail closed);
an explicitly requested lora[] always wins.
"""
import asyncio
import importlib
import json

import httpx
import pytest

from backend.agent import loras
from backend.agent import tools
from backend.agent import config as cfgmod
from backend.db.database import (
    add_message,
    create_conversation,
    get_last_user_message,
)


class _FakeResp:
    status_code = 200
    text = ""

    def json(self):
        return {}


class _FakeClient:
    """Captures the img_gen submit body so tests can assert the wire
    request. Capabilities reply with a small honest server (no hires,
    no upscale) so the flow stays on the native job path."""

    job_bodies: list = []

    @classmethod
    def reset(cls):
        cls.job_bodies = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **k):
        r = _FakeResp()
        if url.endswith("/sdcpp/v1/capabilities"):
            r.json = lambda: {
                "limits": {"min_width": 64, "min_height": 64,
                           "max_width": 2048, "max_height": 2048},
                "defaults": {"width": 512, "height": 512},
                "features_by_mode": {"img_gen": {"hires": False,
                                                 "ref_images": True,
                                                 "lora": True}},
                "loras": [{"name": "ck-neon-retrowave-klein-9b_epoch_10",
                           "path": "ck-neon-retrowave-klein-9b_epoch_10.safetensors"}],
                "upscale": False,
                "upscalers": [],
            }
        elif url.endswith("/sdcpp/v1/jobs/fake-job"):
            r.json = lambda: {
                "status": "completed",
                "result": {"images": [{"b64_json": _b64()}]},
            }
        else:
            r.status_code = 404
            r.json = lambda: {}
        return r

    async def post(self, url, json=None, **k):
        r = _FakeResp()
        if url.endswith("/sdcpp/v1/img_gen"):
            type(self).job_bodies.append(json or {})
            r.status_code = 202
            r.json = lambda: {"id": "fake-job",
                              "poll_url": "/sdcpp/v1/jobs/fake-job"}
        else:
            r.status_code = 404
            r.json = lambda: {}
        return r


def _b64():
    # 1x1 transparent PNG
    return (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
        "YGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
    )


def _scan_with(files: dict) -> dict:
    return {"api_base": "http://127.0.0.1:1234", "files": files}


def _retrowave_scan() -> dict:
    return _scan_with({
        "ck-neon-retrowave-klein-9b_epoch_10.safetensors": {
            "name": "ck-neon-retrowave-klein-9b_epoch_10",
            "class": "triggerless", "trigger": None, "multiplier": None,
            "match": [],
        }
    })


MODEL_PROMPT = (
    "A man walking across a neon blue and black stylized sidewalk in a "
    "highly stylized city, high contrast, cyberpunk aesthetic. The man is "
    "walking, seen from a side view, matching the man in the second "
    "reference image."
)
USER_TYPED = "put the man from image 2 walking across the sidewalk in image 1"


# ------------------------------------------------- the live failure replay

def test_composed_prompt_never_matches():
    """The chat-901 replay: the model's own style words must not summon
    the LoRA; the user's typed words contain no style name."""
    body = {"prompt": MODEL_PROMPT}
    loras.apply_to_body(body, _retrowave_scan(), MODEL_PROMPT,
                        user_text=USER_TYPED)
    assert "lora" not in body
    assert body["prompt"] == MODEL_PROMPT


def test_user_naming_the_style_matches():
    body = {"prompt": "a city street"}
    loras.apply_to_body(body, _retrowave_scan(), "a city street",
                        user_text="make it look retrowave")
    assert body["lora"][0]["path"] == "ck-neon-retrowave-klein-9b_epoch_10.safetensors"
    assert body["lora_matched"] == ["retrowave"]


def test_user_words_hit_all_filename_tokens():
    body = {"prompt": "a poster"}
    loras.apply_to_body(body, _retrowave_scan(), "a poster",
                        user_text="put him on a neon retro-wave poster")
    assert "lora" in body
    assert sorted(body["lora_matched"]) == ["neon", "retrowave"]


def test_no_user_text_is_fail_closed():
    """Tests/direct callers/transports without the injection match
    nothing on content - never the composed prompt (the old behavior)."""
    body = {"prompt": MODEL_PROMPT}
    loras.apply_to_body(body, _retrowave_scan(), MODEL_PROMPT, user_text=None)
    assert "lora" not in body
    body2 = {"prompt": "make it retrowave"}
    loras.apply_to_body(body2, _retrowave_scan(), "make it retrowave")
    assert "lora" not in body2


def test_trigger_injection_requires_user_words():
    """A triggered LoRA's trigger word rides the wire only when the USER
    typed it - the model repeating the trigger in its prompt must not
    inject it."""
    scan = _scan_with({
        "style.safetensors": {
            "name": "style", "class": "triggered",
            "trigger": "elusarca_ancient_style", "multiplier": 0.8,
            "match": ["elusarca_ancient_style"],
        }
    })
    body = {"prompt": "an elusarca_ancient_style marble bust"}
    loras.apply_to_body(body, scan, body["prompt"])
    assert "lora" not in body  # no user text -> no match, no injection
    body2 = {"prompt": "a marble bust"}
    loras.apply_to_body(body2, scan, body2["prompt"],
                        user_text="an elusarca_ancient_style marble bust please")
    assert body2["prompt"].startswith("elusarca_ancient_style, ")


# ------------------------------------------------- attachment stripping

def test_match_text_strips_attachment_scaffolding():
    text = (
        "edit this photo\n\n--- attached file: retrowave-notes.txt ---\n"
        "```\nretrowave style guide\n```\n"
    )
    assert loras._match_text(text) == "edit this photo"


def test_style_word_only_in_attachment_does_not_match():
    body = {"prompt": MODEL_PROMPT}
    loras.apply_to_body(
        body, _retrowave_scan(), MODEL_PROMPT,
        user_text="edit this\n\n--- attached file: retrowave.txt ---\n"
                  "```\nretrowave palette\n```\n",
    )
    assert "lora" not in body


# ------------------------------------------------- harness injection

@pytest.fixture
def configured(tmp_path, monkeypatch):
    """Isolated config with an sd-server configured (same contract as
    test_issue_398's fixture); the persisted scan carries the retrowave
    file so auto-apply has something to consult. Full access mode so the
    loop-level test's generate_image executes without a gate prompt."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    _FakeClient.reset()
    tools._CAPS_CACHE.clear()
    cfgmod.save_config({
        "access_mode": "full",
        "image_generation": {"provider": "sdcpp",
                             "api_base": "http://127.0.0.1:1234"},
        "lora_scan": {"http://127.0.0.1:1234": _retrowave_scan()},
    })


def test_execute_tool_injects_user_text_for_image_gen(configured, tmp_path, monkeypatch):
    """The dispatch funnel passes the user's typed words into the
    executor arguments; the composed prompt's style words cannot fire."""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(tools.execute_tool(
        "generate_image", {"prompt": MODEL_PROMPT},
        workspace=str(tmp_path), user_text=USER_TYPED,
    ))
    assert "error" not in result
    assert "lora" not in _FakeClient.job_bodies[-1]
    assert "matched user words" not in result.get("note", "")


def test_execute_tool_user_words_fire_and_report(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(tools.execute_tool(
        "generate_image", {"prompt": "a city street"},
        workspace=str(tmp_path), user_text="make it look retrowave",
    ))
    assert "error" not in result
    body = _FakeClient.job_bodies[-1]
    assert body["lora"][0]["path"] == "ck-neon-retrowave-klein-9b_epoch_10.safetensors"
    assert "matched user words: retrowave" in result.get("note", "")


def test_execute_tool_without_user_text_does_not_auto_apply(configured, tmp_path, monkeypatch):
    """Fail-closed at the funnel too: no injected words -> no auto-apply
    (this is the old behavior's exact hole)."""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(tools.execute_tool(
        "generate_image", {"prompt": "make it look retrowave"},
        workspace=str(tmp_path),
    ))
    assert "error" not in result
    assert "lora" not in _FakeClient.job_bodies[-1]


def test_explicit_lora_beats_absent_user_text(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(tools.execute_tool(
        "generate_image",
        {"prompt": "a castle", "lora": [{"name": "ck-neon-retrowave-klein-9b_epoch_10"}]},
        workspace=str(tmp_path),
    ))
    assert "error" not in result
    body = _FakeClient.job_bodies[-1]
    assert body["lora"][0]["path"] == "ck-neon-retrowave-klein-9b_epoch_10.safetensors"


def test_edit_image_wrapper_threads_user_text(configured, tmp_path, monkeypatch):
    import base64

    # a real (1x1) reference: edit mode refuses non-existent refs by design
    (tmp_path / "ref.png").write_bytes(base64.b64decode(_b64()))
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(tools.execute_tool(
        "edit_image",
        {"prompt": MODEL_PROMPT,
         "reference_images": ["ref.png"]},
        workspace=str(tmp_path), user_text=USER_TYPED,
    ))
    assert "error" not in result
    body = _FakeClient.job_bodies[-1]
    assert "lora" not in body


# ------------------------------------------------- db helper

async def test_get_last_user_message_returns_typed_text(tmp_path):
    cid = await create_conversation(str(tmp_path), "t")
    await add_message(cid, "user", USER_TYPED)
    await add_message(cid, "user", "second message")
    assert await get_last_user_message(cid) == "second message"


async def test_get_last_user_message_none_without_rows(tmp_path):
    cid = await create_conversation(str(tmp_path), "t")
    assert await get_last_user_message(cid) is None


# ------------------------------------------------- sub-agent boundary

async def test_sub_agents_never_reach_auto_apply(monkeypatch, tmp_path, configured):
    """Image generation is a MAIN-CHAT capability by design
    (subagents._ALWAYS_EXCLUDED keeps generate_image/edit_image out of
    every sub-agent's tool set), so no user_text threading into
    spawn_batch is needed - the boundary itself is the guarantee. This
    guard fails if image-gen tools ever become sub-agent-reachable
    without the user_text injection being added to that path."""
    import backend.agent.subagents as subagents_mod

    defn = subagents_mod.get_agent_def("general-purpose")
    schemas = subagents_mod._resolve_tools(defn, workspace=str(tmp_path))
    names = {s["function"]["name"] for s in schemas}
    assert "generate_image" not in names
    assert "edit_image" not in names
    assert "generate_image" in subagents_mod._ALWAYS_EXCLUDED
    assert "edit_image" in subagents_mod._ALWAYS_EXCLUDED

# ------------------------------------------------- loop-level turn

class _FakeStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


def _tool_call(name, call_id="c1", **args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


async def test_run_agent_turn_matches_user_words_not_model_prompt(
    monkeypatch, tmp_path, configured,
):
    """The full loop shape: a real run_agent turn whose model emits one
    generate_image call with a style-word prompt the user never typed.
    The harness resolves the root user's words and injects them; no
    LoRA may ride along."""
    import backend.agent.loop as loop_mod

    tools._CAPS_CACHE.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    scripts = [
        [
            {"type": "content", "text": "styling"},
            {"type": "tool_calls", "tool_calls": [_tool_call(
                "generate_image", prompt=MODEL_PROMPT)]},
            {"type": "finish"},
        ],
        [{"type": "finish"}],
    ]

    async def fake_chat(messages, tools=None, stream=True):
        return _FakeStream(scripts.pop(0) if scripts else [{"type": "finish"}])

    monkeypatch.setattr(loop_mod.model_client, "chat", fake_chat)

    cid = await create_conversation(str(tmp_path), "t")
    events = []
    async for e in loop_mod.run_agent(
        cid, USER_TYPED, str(tmp_path),
    ):
        events.append(json.loads(e))
    assert "lora" not in _FakeClient.job_bodies[-1]
    assert not any("matched user words" in str(ev) for ev in events)


async def test_run_agent_turn_user_words_still_fire(
    monkeypatch, tmp_path, configured,
):
    """Same loop shape, user typed the style: the auto-apply still
    works through the whole harness chain (regression guard against
    an over-strict fail-closed)."""
    import backend.agent.loop as loop_mod

    tools._CAPS_CACHE.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    scripts = [
        [
            {"type": "content", "text": "styling"},
            {"type": "tool_calls", "tool_calls": [_tool_call(
                "generate_image", prompt="a city street")]},
            {"type": "finish"},
        ],
        [{"type": "finish"}],
    ]

    async def fake_chat(messages, tools=None, stream=True):
        return _FakeStream(scripts.pop(0) if scripts else [{"type": "finish"}])

    monkeypatch.setattr(loop_mod.model_client, "chat", fake_chat)

    cid = await create_conversation(str(tmp_path), "t")
    events = []
    async for e in loop_mod.run_agent(
        cid, "make it look retrowave", str(tmp_path),
    ):
        events.append(json.loads(e))
    body = _FakeClient.job_bodies[-1]
    assert body["lora"][0]["path"] == "ck-neon-retrowave-klein-9b_epoch_10.safetensors"


# ---------------------------------------------------------------------------
# Review round 2 (CodeRabbit findings on PR #440)
# ---------------------------------------------------------------------------


async def test_439_empty_text_turn_falls_back_to_db_row(
    monkeypatch, tmp_path, configured,
):
    """Empty-text turn (an image-only user message): the loop must not
    crash resolving the turn's user text - the old resolve_user_text
    placeholder raised NameError exactly here - and an empty corpus must
    fail closed (no auto-apply)."""
    import backend.agent.loop as loop_mod

    tools._CAPS_CACHE.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    scripts = [
        [
            {"type": "content", "text": "styling"},
            {"type": "tool_calls", "tool_calls": [_tool_call(
                "generate_image", prompt=MODEL_PROMPT)]},
            {"type": "finish"},
        ],
        [{"type": "finish"}],
    ]

    async def fake_chat(messages, tools=None, stream=True):
        return _FakeStream(scripts.pop(0) if scripts else [{"type": "finish"}])

    monkeypatch.setattr(loop_mod.model_client, "chat", fake_chat)

    cid = await create_conversation(str(tmp_path), "t")
    events = []
    async for e in loop_mod.run_agent(
        cid, "", str(tmp_path),  # empty text: the NameError trigger
    ):
        events.append(json.loads(e))
    assert not any(ev.get("type") == "error" for ev in events)
    assert "lora" not in _FakeClient.job_bodies[-1]


async def test_439_steered_message_refreshes_user_text(
    monkeypatch, tmp_path, configured,
):
    """A steered message is the newest user request: once the loop
    consumes it at a tool-result boundary, the match corpus is the
    steer's words. The steer is queued only AFTER the first image call
    has started, so it cannot drain at the pre-tool steer boundary -
    the first image call rides bare on the turn text, the post-steer
    call auto-applies from the steer."""
    import backend.agent.loop as loop_mod

    tools._CAPS_CACHE.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    scripts = [
        [
            {"type": "content", "text": "styling"},
            {"type": "tool_calls", "tool_calls": [_tool_call(
                "generate_image", prompt="a city street")]},
            {"type": "finish"},
        ],
        [
            {"type": "content", "text": "styling again"},
            {"type": "tool_calls", "tool_calls": [_tool_call(
                "generate_image", prompt="a city street", call_id="c2")]},
            {"type": "finish"},
        ],
        [{"type": "finish"}],
    ]

    async def fake_chat(messages, tools=None, stream=True):
        return _FakeStream(scripts.pop(0) if scripts else [{"type": "finish"}])

    monkeypatch.setattr(loop_mod.model_client, "chat", fake_chat)

    cid = await create_conversation(str(tmp_path), "t")
    # The steer must be a genuine mid-run message: queue it only once
    # the first image call has STARTED (its body is already recorded).
    # A pre-run enqueue would drain at the first steer boundary, before
    # tool execution - that ordering is loop behavior, not what this
    # test pins.
    _real_chat = fake_chat

    async def _queue_after_first_call(*_a, **_kw):
        if len(_FakeClient.job_bodies) == 1:
            loop_mod.enqueue_message(cid, "make it look retrowave")
        return await _real_chat(*_a, **_kw)

    loop_mod.model_client.chat = _queue_after_first_call
    async for _e in loop_mod.run_agent(
        cid, "draw something", str(tmp_path),
    ):
        pass
    first, last = _FakeClient.job_bodies[-2], _FakeClient.job_bodies[-1]
    assert "lora" not in first  # turn text named no style
    assert last["lora"][0]["path"] == (
        "ck-neon-retrowave-klein-9b_epoch_10.safetensors"
    )  # the steer's words became the corpus


def test_439_attachment_with_inline_backtick_excluded():
    """An attached file whose body contains single backticks must stay
    excluded from the match corpus: the fence strip must match through
    the closing fence even when the body holds inline code spans."""
    user_text = (
        "edit this\n\n--- attached file: notes.md ---\n"
        "```\nthe file says `sample` retrowave and more\n```\n"
    )
    assert loras._match_text(user_text) == "edit this"
    body = {"prompt": MODEL_PROMPT}
    loras.apply_to_body(
        body, _retrowave_scan(), MODEL_PROMPT, user_text=user_text,
    )
    assert "lora" not in body
