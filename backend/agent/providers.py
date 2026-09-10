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

