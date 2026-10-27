"""Context-window resolution for the per-session context readout.

Resolution order (first hit wins):
1. Settings override for the exact model id (config.context_window_overrides)
2. The provider's own report (Ollama /api/show, probed and cached)
3. The llama.cpp router's /v1/models launch args (--ctx-size, cached)
4. A built-in table of well-known model families (prefix match)
5. None — the UI shows tokens without the % / bar

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

# llama.cpp router /v1/models results, cached the same way (launch args
# are fixed per loaded instance; #578).
_router_cache: dict[str, int | None] = {}
_router_cached_at: dict[str, float] = {}


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
    key = _cache_key(api_base, model)
    if key in _show_cache and now - _show_cached_at.get(key, 0) < _SHOW_TTL:
        return _show_cache[key]
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
    _show_cache[key] = result
    _show_cached_at[key] = now
    if result:
        log.info("context window for %s from ollama /api/show: %d", model, result)
    return result


async def _from_llamacpp_router(api_base: str, api_key: str, model: str) -> int | None:
    """llama.cpp's router exposes each catalog model's launch args on
    /v1/models entries (status.args / status.preset); the --ctx-size value
    there is the loaded server's real n_ctx (#578). The /props endpoint on
    a router reports the ROUTER's n_ctx (0), not the models', so the
    catalog is the only reliable source. Non-router bases (no status.args
    shape) just miss; any failure means 'unknown'."""
    now = time.monotonic()
    key = _cache_key(api_base, model)
    if key in _router_cache and now - _router_cached_at.get(key, 0) < _SHOW_TTL:
        return _router_cache[key]
    result: int | None = None
    try:
        url = api_base.rstrip("/")
        if not url.endswith("/models"):
            url += "/models"
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
        if r.status_code == 200:
            payload = r.json()
            # Shape-validate before field access (#579 review): a 200 with
            # a JSON array or {"data": [null]} must read as 'unknown', not
            # raise AttributeError past the except tuple.
            entries = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(entries, list):
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get("id") != model:
                        continue
                    status = entry.get("status")
                    if not isinstance(status, dict):
                        break
                    result = _ctx_size_from_argv(status.get("args"))
                    if result is None:
                        result = _ctx_size_from_ini(status.get("preset"))
                    break
    except (httpx.HTTPError, ValueError, TypeError):
        result = None
    _router_cache[key] = result
    _router_cached_at[key] = now
    if result:
        log.info("context window for %s from router /v1/models args: %d", model, result)
    return result


def _cache_key(api_base: str, model: str) -> str:
    """Cache identity for the provider-routed probes: (endpoint, model).
    Two providers exposing the same bare model id with different windows
    must not share an entry (CodeRabbit round on PR #579; the same
    identity rule as the Ollama cache below)."""
    return f"{(api_base or '').rstrip('/')}|{model}"


def _ctx_size_from_ini(preset) -> int | None:
    """ctx-size out of a llama.cpp router preset body (ini-style lines,
    `ctx-size = 65535`; also accepts the key with dashes). Returns None
    when absent — never guess."""
    if not isinstance(preset, str):
        return None
    for line in preset.splitlines():
        s = line.strip()
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip().strip('"').strip("'").lower().lstrip("-")
        if k not in {"ctx-size", "ctx.size", "ctxsize"}:
            continue
        try:
            n = int(v.strip().strip('"').strip("'"))
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return None


def _ctx_size_from_argv(lines) -> int | None:
    """--ctx-size out of an argv list or an ini-style preset body.
    Returns None when the flag is absent (never guess a default: a wrong
    window is worse than an unknown one)."""
    if not lines:
        return None
    it = iter(lines)
    for a in it:
        s = str(a).strip()
        val = None
        if s == "--ctx-size":
            val = next(it, None)
        elif s.startswith("--ctx-size="):
            val = s.split("=", 1)[1]
        elif s.startswith("--ctx-size") and len(s) > len("--ctx-size"):
            # llama.cpp single-dash-style glue (--ctx-size65535) is not a
            # real shape, but a bare "--ctx-size" with the value glued on
            # would otherwise read as None.
            val = s[len("--ctx-size"):]
        if val is None:
            continue
        try:
            n = int(str(val).strip().strip('"').strip("'"))
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return None


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
    reported = await _from_llamacpp_router(base, api_key, model)
    if reported:
        return reported
    return _from_table(model)
