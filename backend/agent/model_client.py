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


async def chat(
    messages: list,
    tools: list | None = None,
    stream: bool = False,
) -> dict | AsyncIterator[dict]:
    """Call the model. Returns full response dict, or async iterator of
    streaming deltas if stream=True."""
    cfg = load_config()
    if not cfg["providers"]:
        raise ModelError(
            "No model provider configured. Open Settings and add a provider "
            "(API base, key, model) to start chatting.")
    if not cfg["api_key"] and "openai.com" in cfg["api_base"]:
        raise ModelError("No API key configured. Set AGENT_API_KEY or edit data/config.json")

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg["temperature"],
        "stream": stream,
    }
    # 0/blank = no limit: let the provider use the model's full output cap
    if cfg["max_tokens"] and cfg["max_tokens"] > 0:
        payload["max_tokens"] = cfg["max_tokens"]
    if tools:
        payload["tools"] = tools
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

    return _stream_response(payload, headers)


async def _stream_response(payload: dict, headers: dict) -> AsyncIterator[dict]:
    """Yield parsed SSE chunks: content deltas, tool_call deltas, and a final
    assembled message."""
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream(
            "POST",
            f"{load_config()['api_base'].rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        ) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode("utf-8", errors="replace")
                raise ModelError(f"Model API error {r.status_code}: {body[:500]}")

            tool_calls: dict[int, dict] = {}
            finish_reason = None
            n_content_chars = 0
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                if delta.get("content"):
                    n_content_chars += len(delta["content"])
                    yield {"type": "content", "text": delta["content"]}

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
                "model reply: finish=%s content_chars=%d tool_calls=%d (%s)",
                finish_reason, n_content_chars, len(tool_calls),
                ", ".join(t["function"]["name"] for t in tool_calls.values()) or "-",
            )
            if tool_calls:
                yield {
                    "type": "tool_calls",
                    "tool_calls": [tool_calls[i] for i in sorted(tool_calls)],
                }
