"""Context-window resolution for the per-session context readout.

Resolution order (first hit wins):
1. Settings override for the exact model id (config.context_window_overrides)
2. The provider's own report (Ollama /api/show, probed and cached)
3. A built-in table of well-known model families (prefix match)
4. None — the UI shows tokens without the % / bar

Everything is best-effort by design: a wrong or missing number only costs
the percentage display, never the exact token count (which comes from the
API's usage.prompt_tokens).
"""
import logging
import time

import httpx

log = logging.getLogger("yaah.context")

# model-id prefix (lowercased) -> context window in tokens. The FIRST match
# wins, so more specific families go first. Cover the majors and common
# local families; unknown models simply fall through to None.
_KNOWN_WINDOWS: list[tuple[str, int]] = [
    # OpenAI — most specific first
    ("gpt-4.1", 1_000_000),
    ("o3", 200_000),
    ("o4-mini", 200_000),
    ("o1", 200_000),
    ("gpt-4o", 128_000),
    ("gpt-4.5", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-4", 8_192),
    ("gpt-3.5-turbo", 16_385),
    # Anthropic via OpenAI-compatible gateways (OpenRouter etc.)
    ("anthropic/claude-opus-4", 200_000),
    ("anthropic/claude-sonnet-4", 200_000),
    ("anthropic/claude-3.7", 200_000),
    ("anthropic/claude-3.5", 200_000),
    ("anthropic/claude-3", 200_000),
    ("claude-opus-4", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-3.7", 200_000),
    ("claude-3.5", 200_000),
    ("claude-3", 200_000),
    # Google
    ("gemini-2.5-pro", 1_048_576),
    ("gemini-2.5-flash", 1_048_576),
    ("gemini-2.0", 1_048_576),
    ("gemini-1.5-pro", 2_097_152),
    ("gemini-1.5", 8_192),
    # Meta Llama (Ollama-style bare ids and OpenRouter namespaced ones)
    ("llama4", 128_000),
    ("llama3.3", 128_000),
    ("llama3.2", 128_000),
    ("llama3.1", 128_000),
    ("llama3", 8_192),
    # Mistral
    ("mistral-large", 128_000),
    ("mistral-medium", 128_000),
    ("mistral-small", 128_000),
    ("mixtral", 32_768),
    ("codestral", 256_000),
    # Other locals
    ("qwen3", 128_000),
    ("qwen2.5", 128_000),
    ("deepseek-r1", 128_000),
    ("deepseek-chat", 64_000),
    ("deepseek-reasoner", 64_000),
    ("phi-4", 128_000),
    ("phi-3", 128_000),
    ("gemma3", 128_000),
    ("gemma2", 8_192),
]

# Ollama /api/show results, cached per model id (context_length never
# changes while the server runs unless the user re-creates the model —
# a backend restart refreshes it).
_show_cache: dict[str, int | None] = {}
_SHOW_TTL = 300.0
_show_cached_at: dict[str, float] = {}


def _from_table(model: str | None) -> int | None:
    if not model:
        return None
    m = model.lower()
    for prefix, window in _KNOWN_WINDOWS:
        if m.startswith(prefix):
            return window
    return None


async def _from_ollama(api_base: str, api_key: str, model: str) -> int | None:
    """Ollama's OpenAI-compatible shim doesn't expose context length via
    /v1/models, but its native /api/show reports context_length. Only worth
    trying on Ollama-shaped bases; any failure just means 'unknown'."""
    if ":11434" not in api_base:
        return None
    now = time.monotonic()
    if model in _show_cache and now - _show_cached_at.get(model, 0) < _SHOW_TTL:
        return _show_cache[model]
    native = api_base.split("/v1")[0].rstrip("/")
    result: int | None = None
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.post(
                f"{native}/api/show",
                json={"model": model},
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
        if r.status_code == 200:
            info = r.json().get("info") or {}
            # Newer Ollama nests it as model_info.<family>.context_length
            for v in info.values():
                if isinstance(v, dict) and v.get("context_length"):
                    result = int(v["context_length"])
                    break
            if result is None and info.get("context_length"):
                result = int(info["context_length"])
    except (httpx.HTTPError, ValueError, TypeError):
        result = None
    _show_cache[model] = result
    _show_cached_at[model] = now
    if result:
        log.info("context window for %s from ollama /api/show: %d", model, result)
    return result


async def get_context_window(
    model: str | None,
    cfg: dict | None = None,
    api_base: str | None = None,
    api_key: str = "",
) -> int | None:
    """Resolve the context window for a model id (see module docstring)."""
    if not model:
        return None
    cfg = cfg or {}
    # Per-model Settings map first (model id -> {context_window}); the
    # legacy flat map is the fallback for pre-upgrade rows.
    mc = cfg.get("model_context") or {}
    override = (mc.get(model) or {}).get("context_window")
    if not override:
        override = (cfg.get("context_window_overrides") or {}).get(model)
    if override:
        try:
            return int(override)
        except (TypeError, ValueError):
            pass
    base = api_base or (cfg.get("api_base") or "")
    reported = await _from_ollama(base, api_key, model)
    if reported:
        return reported
    return _from_table(model)
