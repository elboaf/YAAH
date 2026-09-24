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


LIST_TIMEOUT = 4.0  # seconds per provider (Q8 lean)


def _model_info(models: object) -> list[dict]:
    """Normalize model ids and reasoning effort metadata from /models.

    OpenRouter publishes exact supported efforts under ``reasoning``. Other
    OpenAI-compatible catalogs may publish only ``supported_parameters``;
    that confirms the parameter exists but does not enumerate valid values,
    so no effort choices can safely be offered. Catalogs with no capability
    metadata are unknown, not evidence that the model supports reasoning.
    """
    if not isinstance(models, list):
        return []
    result = []
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"]:
            continue
        reasoning = model.get("reasoning")
        efforts = reasoning.get("supported_efforts") if isinstance(reasoning, dict) else None
        if isinstance(efforts, list):
            efforts = list(dict.fromkeys(v for v in efforts if isinstance(v, str) and v))
        else:
            efforts = []
        parameters = model.get("supported_parameters")
        supports_reasoning = bool(efforts) or bool(reasoning) or (
            isinstance(parameters, list) and "reasoning_effort" in parameters
        )
        result.append(
            {
                "id": model["id"],
                "reasoning_efforts": efforts,
                "supports_reasoning": supports_reasoning,
            }
        )
    return result


async def list_all_models(providers: dict) -> dict:
    """Query every configured provider in parallel.

    Returns {'providers': {name: {'models': [...], 'error'?: str}},
    'model': active_model}. Keys never leave the backend.
    """
    import asyncio

    async def one(api_base: str, api_key: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=LIST_TIMEOUT) as client:
                r = await client.get(
                    f"{api_base.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                )
            if r.status_code != 200:
                return {"models": [], "error": f"HTTP {r.status_code}"}
            model_info = _model_info(r.json().get("data", []))
            models = sorted(item["id"] for item in model_info)
            return {"models": models, "model_info": sorted(model_info, key=lambda item: item["id"])}
        except (httpx.HTTPError, ValueError) as e:
            return {"models": [], "error": str(e)}

    results = await asyncio.gather(
        *(one(p["api_base"], p.get("api_key") or "") for p in providers.values())
    )
    return {"providers": dict(zip(providers.keys(), results))}


async def list_models(api_base: str, api_key: str = "") -> dict:
    """Query {api_base}/models. Returns {'models': [id...]} or {'error'}."""
    url = f"{api_base.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}", "models": []}
        model_info = _model_info(r.json().get("data", []))
        models = sorted(item["id"] for item in model_info)
        return {"models": models, "model_info": sorted(model_info, key=lambda item: item["id"])}
    except (httpx.HTTPError, ValueError) as e:
        return {"error": str(e), "models": []}

