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
    keeps the active provider; "provider::model" routes the call at that
    configured provider (base + key + model all swap).

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
        else:
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
            async with httpx.AsyncClient(timeout=120) as client:
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
            raise ModelError(f"Model API error {r.status_code}: {r.text[:500]}")
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


async def _stream_response(
    payload: dict,
    headers: dict,
    api_base: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> AsyncIterator[dict]:
    """Yield parsed SSE chunks: content deltas, tool_call deltas, and a final
    assembled message. api_base None = resolve from the live config (legacy
    direct callers). Opens with a model_call event (issue #43) so the UI can
    show "waiting for <provider>" between send and first token.

    provider/model: the RESOLVED call scope (per-chat override applied, #51/#76).
    The event must report what this call actually targets — re-reading the
    global config here made the waiting readout follow the sidebar default
    instead of the chat's pinned model."""
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
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream(
                "POST",
                f"{api_base.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            ) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", errors="replace")
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
        yield {
            "type": "tool_calls",
            "tool_calls": [tool_calls[i] for i in sorted(tool_calls)],
        }
