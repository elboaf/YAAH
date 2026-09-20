"""Model client: OpenAI-compatible chat completions with tool calling.

Works with OpenAI, OpenRouter, Ollama, LM Studio, or any compatible endpoint.
Supports both blocking and streaming responses.
"""
import json
import logging
from typing import AsyncIterator

import httpx

from backend.agent.config import load_config

# Diagnostics go to stderr → %TEMP%/yaah-backend.log (tee'd by the Tauri
# shell), so "model ignored the tools" vs "tools never sent" vs "server
# dropped them" is answerable from the log on any machine.
log = logging.getLogger("yaah.model")


class ModelError(Exception):
    pass


def _build_payload(cfg: dict, tools: list | None, stream: bool) -> dict:
    """The chat-completions request body. Pure so the param-gating rules are
    unit-testable without HTTP."""
    payload = {
        "model": cfg["model"],
        "messages": [],
        "temperature": cfg["temperature"],
        "stream": stream,
    }
    # 0/blank = no limit: let the provider use the model's full output cap
    if cfg["max_tokens"] and cfg["max_tokens"] > 0:
        payload["max_tokens"] = cfg["max_tokens"]
    # Reasoning effort (#6): "" = don't send the param at all, so providers
    # that hard-reject unknown fields are unaffected until the user opts in
    # (Settings: Default / low / medium / high). Only reasoning-capable
    # models react to it; others ignore or 400 — documented in Settings.
    effort = (cfg.get("reasoning_effort") or "").strip().lower()
    if effort in ("low", "medium", "high"):
        payload["reasoning_effort"] = effort
    if stream:
        # Ask for exact usage (usage.prompt_tokens = what this call actually
        # fed the model) even in streaming mode. OpenAI-compatible servers
        # that don't know the option just ignore it.
        payload["stream_options"] = {"include_usage": True}
    if tools:
        payload["tools"] = tools
    return payload


async def chat(
    messages: list,
    tools: list | None = None,
    stream: bool = False,
    model: str = "",
    effort: str = "",
) -> dict | AsyncIterator[dict]:
    """Call the model. Returns full response dict, or async iterator of
    streaming deltas if stream=True.

    model/effort: per-call overrides for scheduled agents (issue #41) —
    empty strings mean "use the active global model / effort setting".
    A per-agent model may be "provider::model" to route the run at a
    specific configured provider (per-agent model picker); a bare id
    keeps the active provider and only swaps the model name."""
    cfg = dict(load_config())
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
    if effort:
        cfg["reasoning_effort"] = effort
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
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{cfg['api_base'].rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
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
    return _stream_response(payload, headers, cfg["api_base"])


async def _stream_response(
    payload: dict, headers: dict, api_base: str | None = None
) -> AsyncIterator[dict]:
    """Yield parsed SSE chunks: content deltas, tool_call deltas, and a final
    assembled message. api_base None = resolve from the live config (legacy
    direct callers)."""
    if api_base is None:
        api_base = load_config()["api_base"]
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

            tool_calls: dict[int, dict] = {}
            finish_reason = None
            usage: dict | None = None
            n_content_chars = 0
            saw_done = False
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
                # for the transcript — the UI telemetry tape consumes them.
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
                    "model stream ended without a finish reason — the "
                    "connection was likely dropped mid-response"
                )
            if usage and usage.get("prompt_tokens") is not None:
                yield {"type": "usage", "usage": usage}
            if tool_calls:
                yield {
                    "type": "tool_calls",
                    "tool_calls": [tool_calls[i] for i in sorted(tool_calls)],
                }
