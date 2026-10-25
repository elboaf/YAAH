"""Model client: OpenAI-compatible chat completions with tool calling.

Works with OpenAI, OpenRouter, Ollama, LM Studio, or any compatible endpoint.
Supports both blocking and streaming responses.
"""
import json
import logging
import time
from typing import AsyncIterator

import httpx

from backend.agent.config import load_config

# Diagnostics go to stderr → %TEMP%/yaah-backend.log (tee'd by the Tauri
# shell), so "model ignored the tools" vs "tools never sent" vs "server
# dropped them" is answerable from the log on any machine.
log = logging.getLogger("yaah.model")


class ModelError(Exception):
    pass


class ModelTimeout(ModelError):
    """The provider never responded within the timeout window (issue #43):
    rendered as "no response from <provider> after <n>s" instead of a raw
    httpx error string, so a hung call is distinguishable from any other
    API failure."""


class ModelCancelled(ModelError):
    """The in-flight provider call was cancelled by the user (Stop button,
    issue #389). Distinguished from a provider failure so the run loop can
    settle the turn as 'stopped' instead of surfacing an API error."""


class ModelImageRejected(ModelError):
    """The provider rejected the request because it cannot accept image
    input (issue #428). llama.cpp-class servers without a mmproj validate
    the request before prefill and reject the WHOLE call with
    "image input is not supported - hint: ... provide the mmproj"; the
    caller's auto-retry (Q23) is byte-identical, so it fails the same way
    and the turn dies before the model ever reads the store-rel annotation
    that would let it wire generate_image. Distinguished so the caller can
    strip the image parts (the text parts - with the #406 store-rel
    annotations - survive) and retry text-only."""


# Image-input rejection markers (issue #428). Matched against the raw error
# body, case-insensitively: llama.cpp "image input is not supported - hint:
# ... provide the mmproj", Ollama "not support image", LM Studio variants.
IMAGE_REJECT_MARKERS = (
    "image input is not supported",
    "provide the mmproj",
    "mmproj",
    "not support image",
    "does not support images",
    "image is not supported",
    "unsupported image",
    "vision is not supported",
    "vision not supported",
    "not a multimodal",
    "no multimodal",
)


def _looks_like_image_rejection(status: int, body: str) -> bool:
    """Heuristic over the error body: a non-200 whose text matches an
    image/vision/mmproj rejection marker. Deliberately loose - a false
    positive costs one text-only retry of an already-failing call; a false
    negative re-creates the dead turn."""
    if status == 200:
        return False
    low = (body or "").lower()
    return any(marker in low for marker in IMAGE_REJECT_MARKERS)


# The note a non-vision context carries (issue #428). Exported so the
# reactive retry and the #430 proactive pre-strip say the SAME thing to the
# model through the SAME helper - one wording to maintain.
IMAGE_REJECTION_NOTE = (
    "The user attached image(s), but the current model "
    "provider rejected image input, so the image bytes "
    "were removed from this context. The store rel path "
    "annotations under the attached images remain valid: "
    "pass those paths to generate_image (mode \"edit\", "
    "reference_images) or upscale_image (path) - they resolve the stored "
    "files server-side without vision."
)

# Generic wording for the #430 proactive path (the provider did not reject
# anything - it merely reports no vision).
IMAGE_NOVISION_NOTE = (
    "The current model provider reports no vision support, so the image "
    "bytes were left out of this context. The store rel path annotations "
    "under the attached images remain valid: pass those paths to "
    "generate_image (mode \"edit\", reference_images) or "
    "upscale_image (path) - they resolve the stored files "
    "server-side without vision."
)


def merge_note_into_first_system(messages: list, note: str) -> None:
    """Fold `note` into the FIRST system message in place - never a second
    system row: strict chat templates (llama.cpp qwen-class) reject any
    system message after the first (#428 contract, asserted by the #428
    e2e test). Transient by design: the frozen head on the conversation row
    replays unchanged next turn (KV-cache stability, #375). Mutates the
    first message's content string; inserts a system row only when the
    context has none at all."""
    idx = next(
        (i for i, m in enumerate(messages) if m.get("role") == "system"),
        None,
    )
    if idx is None:
        messages.insert(0, {"role": "system", "content": note})
        return
    head = messages[idx]
    content = head.get("content")
    if isinstance(content, list):
        # Parts-list head (rare: a system row built as a parts list):
        # collapse to its text parts rather than str()-ing the structure.
        texts = [
            str(p.get("text") or "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        content = "\n".join(texts)
    head["content"] = (content or "") + "\n\n" + note


def has_image_parts(messages: list) -> bool:
    """True when any message carries an image_url part (parts-list content).
    Cheap scan for the #430 turn seam - don't probe providers that have
    nothing to strip."""
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, list) and any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for p in content
        ):
            return True
    return False


def strip_image_parts(messages: list) -> tuple[list, int]:
    """Return (copy_of_messages_without_image_url_parts, count_stripped).

    Deep-copies each message dict and keeps ONLY the text parts of parts-list
    contents (the text parts carry the #406 store-rel annotations a
    non-vision model still needs to wire generate_image/upscale_image).
    String contents and tool_calls/tool/assistant/system rows pass through
    untouched. Pure function so the strip rules are unit-testable.
    """
    out: list = []
    stripped = 0
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    stripped += 1
                    continue
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(str(part.get("text") or ""))
                    continue
                if isinstance(part, dict):
                    texts.append(json.dumps(part))
                else:
                    texts.append(str(part))
            new_m = dict(m)
            new_m["content"] = "\n".join(texts)
            out.append(new_m)
        else:
            out.append(dict(m) if isinstance(m, dict) else m)
    return out, stripped


# llama.cpp (and friends) send zero response bytes until prompt prefill
# finishes, so the httpx READ timeout must cover the whole prefill, not the
# inter-chunk gap: at ~40 tok/s a 128k-token cold prefill runs ~55 minutes.
# A short read timeout doesn't just fail the request — when the caller
# retries, the server gets a new prompt mid-prefill and never converges.
# 3600 s reads are still bounded so a dead socket eventually surfaces;
# connect/write/pool stay tight.
CHAT_CONNECT_TIMEOUT = 10
CHAT_READ_TIMEOUT = 3600
CHAT_WRITE_TIMEOUT = 60
CHAT_POOL_TIMEOUT = 10


def _chat_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=CHAT_CONNECT_TIMEOUT,
        read=CHAT_READ_TIMEOUT,
        write=CHAT_WRITE_TIMEOUT,
        pool=CHAT_POOL_TIMEOUT,
    )



def _provider_from_base(api_base: str) -> str:
    """A short provider label for UI readouts, derived from the API base."""
    try:
        host = api_base.split("//", 1)[-1].split("/", 1)[0].lower()
    except (AttributeError, IndexError):
        return api_base or "provider"
    if "openrouter" in host:
        return "openrouter"
    if "openai" in host:
        return "openai"
    if "anthropic" in host:
        return "anthropic"
    if "ollama" in host or "localhost" in host or "127.0.0.1" in host:
        return "local"
    return host or "provider"


def _classify_timeout(e: Exception, provider: str, started: float) -> ModelTimeout:
    elapsed = int(time.monotonic() - started)
    return ModelTimeout(
        f"no response from {provider} after {elapsed}s "
        f"(timeout) — the provider may be hung, offline, or unreachable"
    )


def _build_payload(cfg: dict, tools: list | None, stream: bool) -> dict:
    """The chat-completions request body. Pure so the param-gating rules are
    unit-testable without HTTP."""
    # Let the selected model/provider apply its own generation defaults and
    # output cap. Temperature and max_tokens are intentionally not sent.
    payload = {
        "model": cfg["model"],
        "messages": [],
        "stream": stream,
    }
    # Reasoning effort is set per chat (or inherited from the new-chat
    # default in the sidebar); blank means use the provider default.
    effort = (cfg.get("reasoning_effort") or "").strip().lower()
    if effort and len(effort) <= 64:
        payload["reasoning_effort"] = effort
    if stream:
        # Ask for exact usage (usage.prompt_tokens = what this call actually
        # fed the model) even in streaming mode. OpenAI-compatible servers
        # that don't know the option just ignore it.
        payload["stream_options"] = {"include_usage": True}
    if tools:
        payload["tools"] = tools
    return payload


def _resolve_call_cfg(cfg: dict, model: str = "", effort: str | None = "") -> dict:
    """Apply per-call model/effort overrides to a config copy. Pure so the
    precedence rules are unit-testable without HTTP.

    model: "" = the active global model; a bare id swaps the model name and
    keeps the active provider (back-compat branch — storage must never
    write this shape, see config.qualify_model_scope/#132);
    "provider::model" routes the call at that configured provider (base +
    key + model all swap, and the copy's active_provider is resolved to the
    targeted provider so consumers like the model_call event report the
    truth, not the ambient default).

    effort: "" = inherit the global reasoning_effort setting; None = THIS
    call explicitly sends no reasoning_effort param (#51/#76 — a chat whose
    stamped effort is '' chose Default deliberately; blank must not drag
    the global back in); any non-empty provider-advertised value overrides.
    """
    cfg = dict(cfg)
    if model:
        provider_name, sep, model_id = model.partition("::")
        if sep:
            prov = (cfg.get("providers") or {}).get(provider_name)
            if not prov:
                raise ModelError(
                    f"Unknown provider '{provider_name}' in model override "
                    f"'{model}'. Check Settings → Providers.")
            cfg["api_base"] = prov.get("api_base") or ""
            cfg["api_key"] = prov.get("api_key") or ""
            cfg["model"] = model_id
            # #132: report the RESOLVED routing, not the ambient default —
            # the model_call event ("waiting for <provider>") and any other
            # consumer must name the provider this call actually targets.
            cfg["active_provider"] = provider_name
        else:
            # Bare ids stay the back-compat branch: the model swaps, the
            # ambient provider does too. Storage must never write this
            # shape (see qualify_model_scope) — a bare scope has no routing
            # provider and drifts with the sidebar default.
            cfg["model"] = model
    if effort is None:
        cfg["reasoning_effort"] = ""
    elif effort:
        cfg["reasoning_effort"] = effort.strip().lower()
    return cfg


async def chat(
    messages: list,
    tools: list | None = None,
    stream: bool = False,
    model: str = "",
    effort: str | None = "",
) -> dict | AsyncIterator[dict]:
    """Call the model. Returns full response dict, or async iterator of
    streaming deltas if stream=True.

    model/effort: per-call overrides for scheduled agents (issue #41) and,
    since #51/#76, for per-chat scoping — see _resolve_call_cfg for the
    precedence (effort None = the reasoning_effort param is explicitly NOT
    sent this call, regardless of the global setting)."""
    cfg = _resolve_call_cfg(dict(load_config()), model, effort)
    if not cfg["providers"]:
        raise ModelError(
            "No model provider configured. Open Settings and add a provider "
            "(API base, key, model) to start chatting.")
    if not cfg["api_key"] and "openai.com" in cfg["api_base"]:
        raise ModelError("No API key configured. Set AGENT_API_KEY or edit data/config.json")

    payload = _build_payload(cfg, tools, stream)
    payload["messages"] = messages
    log.info("model call: base=%s model=%s tools_sent=%d stream=%s",
             cfg["api_base"], cfg["model"], len(tools or []), stream)

    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}

    if not stream:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_chat_timeout()) as client:
                r = await client.post(
                    f"{cfg['api_base'].rstrip('/')}/chat/completions",
                    json=payload,
                    headers=headers,
                )
        except httpx.TimeoutException as e:
            raise _classify_timeout(
                e,
                cfg.get("active_provider") or _provider_from_base(cfg["api_base"]),
                started,
            ) from e
        if r.status_code != 200:
            body = r.text[:500]
            if _looks_like_image_rejection(r.status_code, body):
                # Issue #428: the provider refuses image input (request-level
                # validation, before any prefill). Classified so the caller
                # can retry text-only instead of burning the byte-identical
                # retry on the same rejection.
                raise ModelImageRejected(f"Model API error {r.status_code}: {body}")
            raise ModelError(f"Model API error {r.status_code}: {body}")
        data = r.json()
        log.info("model reply: finish=%s tool_calls=%d",
                 (data.get("choices") or [{}])[0].get("finish_reason"),
                 len((data.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []))
        return data

    # The payload already carries the per-call model/effort overrides; the
    # resolved api_base rides along explicitly so a provider::model override
    # targets the right host (the old re-read would have hit the active one).
    return _stream_response(
        payload, headers, cfg["api_base"],
        provider=cfg.get("active_provider"), model=cfg["model"],
    )


def _stream_response(
    payload: dict,
    headers: dict,
    api_base: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> "CancellableStream":
    """Closable async iterator of parsed SSE chunks: content deltas,
    tool_call deltas, and a final assembled message. api_base None = resolve
    from the live config (legacy direct callers). Opens with a model_call
    event (issue #43) so the UI can show "waiting for <provider>" between
    send and first token.

    provider/model: the RESOLVED call scope (per-chat override applied,
    #51/#76). The event must report what this call actually targets —
    re-reading the global config here made the waiting readout follow the
    sidebar default instead of the chat's pinned model.

    Issue #389: the iterator carries aclose() so a Stop can tear down the
    HTTP stream without draining it — the httpx client/stream `async with`
    exits only run when the generator body unwinds."""
    gen = _stream_gen(payload, headers, api_base, provider, model)
    return CancellableStream(gen)


class CancellableStream:
    """Async iterator delegating to an inner generator, with an explicit
    aclose() (issue #389): Stop must be able to release the provider
    connection immediately instead of waiting for the stream (or a GC
    finalizer) to end it."""

    def __init__(self, gen):
        self._gen = gen

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._gen.__anext__()

    async def aclose(self):
        await self._gen.aclose()


async def _stream_gen(
    payload: dict,
    headers: dict,
    api_base: str | None,
    provider: str | None,
    model: str | None,
):
    if api_base is None:
        api_base = load_config()["api_base"]
    if provider is None or model is None:
        cfg = load_config()
        provider = provider if provider is not None else (
            cfg.get("active_provider") or _provider_from_base(api_base)
        )
        model = model if model is not None else cfg["model"]
    yield {"type": "model_call", "provider": provider, "model": model}
    started = time.monotonic()
    tool_calls: dict[int, dict] = {}
    finish_reason = None
    usage: dict | None = None
    n_content_chars = 0
    saw_done = False
    try:
        async with httpx.AsyncClient(timeout=_chat_timeout()) as client:
            async with client.stream(
                "POST",
                f"{api_base.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            ) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", errors="replace")
                    if _looks_like_image_rejection(r.status_code, body):
                        raise ModelImageRejected(
                            f"Model API error {r.status_code}: {body[:500]}"
                        )
                    raise ModelError(f"Model API error {r.status_code}: {body[:500]}")

                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        saw_done = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # OpenAI (with stream_options.include_usage) sends a final
                    # choices-less chunk carrying usage; Ollama puts usage on the
                    # last chunk alongside finish_reason. Capture either.
                    if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    if delta.get("content"):
                        n_content_chars += len(delta["content"])
                        yield {"type": "content", "text": delta["content"]}

                    # Reasoning models (GLM, DeepSeek-R1, ...) stream their
                    # thinking as reasoning_content/reasoning deltas. Not kept
                    # for the transcript \u2014 the UI telemetry tape consumes them.
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning and isinstance(reasoning, str):
                        yield {"type": "thinking", "text": reasoning}

                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls.setdefault(
                            idx,
                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]

                    finish = choices[0].get("finish_reason")
                    if finish:
                        finish_reason = finish
                        yield {"type": "finish", "reason": finish}
    except httpx.TimeoutException as e:
        # Issue #43: a provider that never answers (hung, offline,
        # unreachable) classified as its own failure mode instead of
        # a raw httpx error string.
        raise _classify_timeout(e, provider, started) from e

    log.info(
        "model reply: finish=%s content_chars=%d tool_calls=%d prompt_tokens=%s (%s)",
        finish_reason, n_content_chars, len(tool_calls),
        (usage or {}).get("prompt_tokens"),
        ", ".join(t["function"]["name"] for t in tool_calls.values()) or "-",
    )
    if not saw_done and finish_reason is None:
        # The connection closed before the model finished (no [DONE],
        # no finish_reason). Treating this as a completed answer used
        # to end the turn silently with whatever partial content
        # arrived; surface it as a retryable error instead.
        raise ModelError(
            "model stream ended without a finish reason \u2014 the "
            "connection was likely dropped mid-response"
        )
    if usage and usage.get("prompt_tokens") is not None:
        yield {"type": "usage", "usage": usage}
    if tool_calls:
        # A provider glitch (or a hung stream that emitted only the slot
        # initializer) can leave a call with an empty id or name. Persisting
        # or replaying it poisons the history \u2014 every later turn then fails
        # with "tool messages must include a non-empty string tool_call_id".
        # Drop degenerate slots; an emission that becomes empty becomes a
        # normal no-tool-call reply.
        live = [
            tool_calls[i]
            for i in sorted(tool_calls)
            if tool_calls[i].get("id") and tool_calls[i].get("function", {}).get("name")
        ]
        if live:
            yield {"type": "tool_calls", "tool_calls": live}
