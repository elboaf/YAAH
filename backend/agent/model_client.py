"""Model client: OpenAI-compatible chat completions with tool calling.

Works with OpenAI, OpenRouter, Ollama, LM Studio, or any compatible endpoint.
Supports both blocking and streaming responses.
"""
import json
from typing import AsyncIterator

import httpx

from backend.agent.config import load_config


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
    if not cfg["api_key"] and "openai.com" in cfg["api_base"]:
        raise ModelError("No API key configured. Set AGENT_API_KEY or edit data/config.json")

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg["temperature"],
        "max_tokens": cfg["max_tokens"],
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools

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
            return r.json()

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
                    yield {"type": "finish", "reason": finish}

            if tool_calls:
                yield {
                    "type": "tool_calls",
                    "tool_calls": [tool_calls[i] for i in sorted(tool_calls)],
                }
