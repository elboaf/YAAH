"""Provider presets and model discovery for OpenAI-compatible servers.

Covers local stacks (Ollama, LM Studio, llama.cpp server) and hosted
providers (OpenAI, OpenRouter). All speak the OpenAI /v1 protocol.
"""
import httpx

# name -> (default api_base, default model, needs_api_key, supports_tools)
PRESETS = {
    "ollama": {
        "api_base": "http://localhost:11434/v1",
        "model": "llama3.2",
        "needs_api_key": False,
        "supports_tools": True,
    },
    "lmstudio": {
        "api_base": "http://localhost:1234/v1",
        "model": "local-model",
        "needs_api_key": False,
        "supports_tools": True,
    },
    "llamacpp": {
        "api_base": "http://localhost:8080/v1",
        "model": "local-model",
        "needs_api_key": False,
        # llama.cpp server tool support depends on build/flags; probe it
        "supports_tools": None,
    },
    "openai": {
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "needs_api_key": True,
        "supports_tools": True,
    },
    "openrouter": {
        "api_base": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "needs_api_key": True,
        "supports_tools": True,
    },
}


def detect_preset(api_base: str) -> str | None:
    """Guess which preset an api_base belongs to."""
    base = (api_base or "").lower()
    for name, p in PRESETS.items():
        if p["api_base"].lower().rstrip("/v1") in base or p["api_base"].lower() in base:
            return name
    return None


async def list_models(api_base: str, api_key: str = "") -> dict:
    """Query {api_base}/models. Returns {'models': [id...]} or {'error'}."""
    url = f"{api_base.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}", "models": []}
        data = r.json().get("data", [])
        models = [
            m.get("id", "") for m in data if isinstance(m, dict) and m.get("id")
        ]
        return {"models": sorted(models)}
    except (httpx.HTTPError, ValueError) as e:
        return {"error": str(e), "models": []}


# Tool-capability probing (Q35): cached per api_base+model.
_probe_cache: dict[str, bool] = {}


async def probe_tool_support(api_base: str, model: str, api_key: str = "") -> bool:
    """Cheaply probe whether an endpoint/model accepts tool calls.

    Sends a minimal chat request with a dummy tool and checks whether the
    response is accepted. Result cached per api_base+model for the session.
    """
    key = f"{api_base}|{model}"
    if key in _probe_cache:
        return _probe_cache[key]

    supported = False
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "noop",
                    "description": "do nothing",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{api_base.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
        # 200 => endpoint accepted tools; 4xx mentioning tools => not supported
        if r.status_code == 200:
            supported = True
    except httpx.HTTPError:
        supported = False

    _probe_cache[key] = supported
    return supported


def capability_for(api_base: str, model: str) -> bool | None:
    """Known capability from presets, or None if it must be probed."""
    preset = detect_preset(api_base)
    if preset and PRESETS[preset]["supports_tools"] is not None:
        return PRESETS[preset]["supports_tools"]
    return _probe_cache.get(f"{api_base}|{model}")
