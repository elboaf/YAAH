"""Vision-capability probe (issue #430): know before the first model call
whether a provider can accept image input.

Advisory ONLY. The probe exists to AVOID a failure, never to cause one:
- a verdict of True or False comes from a rule with definitive evidence
  (a server endpoint, an instance's launch argv, a curated catalog);
- None (unknown) must behave EXACTLY like the pre-#430 world - pixels are
  sent, and #428's reactive image-rejection retry remains the backstop;
- the reactive path feeds the cache (record_rejection): a provider that
  rejected images PROVED the negative, and that is worth remembering. A
  successful call proves nothing and is never cached.

Rules are tried in order; the first rule that returns a verdict (True or
False) wins. A rule that cannot decide returns None - that is what makes
"unknown" safe. All rules are best-effort: network errors, missing fields,
and unknown server shapes all collapse to None.
"""
import asyncio
import json
import time
from typing import Callable, Literal, Optional

import httpx

Verdict = Optional[bool]  # True / False / None (= unknown)

PROBE_TIMEOUT = 3.0        # per-rule HTTP budget (seconds)
CAP_BUDGET = 0.3           # cap on the turn-seam await (see capability_capped)
_TTL = 300.0               # definitive verdicts re-probe after 5 minutes
                           # (autoload routers can swap instances mid-session)
_NEG_TTL = 60.0            # failed/unknown probes are remembered briefly, so
                           # a dead server is not probed on every turn
_REJECT_TTL = 3600.0       # a RECORDED rejection is deterministic evidence:
                           # trust it for an hour before probing again

from backend.agent.config import load_config


class _Entry:
    __slots__ = ("verdict", "at", "ttl", "source")

    def __init__(self, verdict: Verdict, ttl: float, source: str):
        self.verdict = verdict
        self.at = time.monotonic()
        self.ttl = ttl
        self.source = source

    def expired(self) -> bool:
        return (time.monotonic() - self.at) > self.ttl


_CACHE: dict[tuple[str, str], _Entry] = {}
# Single-flight for the OpenRouter catalog fetch (one GET per TTL window,
# however many models ask concurrently).
_catalog_lock: Optional[asyncio.Lock] = None


def _catalog() -> asyncio.Lock:
    global _catalog_lock
    if _catalog_lock is None:
        _catalog_lock = asyncio.Lock()
    return _catalog_lock


def _cfg() -> dict:
    """The provider config for (api_base, model): api key + resolved model
    id. Live-read like the rest of YAAH so a Settings save applies without
    a restart."""
    try:
        return load_config()
    except Exception:  # noqa: BLE001 - probes never break a turn
        return {}


def _lookup(cfg: dict, api_base: str, model: str) -> tuple[str, str]:
    """Resolve (api_key, request_model) for an (api_base, model) pair.
    The model id is passed through UNCHANGED: the turn scope already hands
    us the exact model being called, and capability is a property of that
    model - substituting the provider's DEFAULT model here probed the wrong
    model on router servers (e.g. asked about gemma4-unc, probed "m") and
    manufactured false unknowns. Only the api_key is resolved from config."""
    for pc in (cfg.get("providers") or {}).values():
        if not isinstance(pc, dict):
            continue
        base = str(pc.get("api_base") or "").rstrip("/")
        if base and base == api_base.rstrip("/"):
            return str(pc.get("api_key") or ""), model
    return "", model


async def _get_json(
    api_base: str,
    path: str,
    api_key: str,
) -> Optional[dict | list]:
    """GET {api_base}{path} and parse JSON; None on anything imperfect."""
    url = api_base.rstrip("/") + path
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001 - probes never break a turn
        return None


# ---------------------------------------------------------------- rules
# Each rule: async (api_base, model, api_key) -> Verdict.
# Return None when the rule cannot decide. Never raise.


async def _rule_llamacpp_props(api_base: str, model: str, api_key: str) -> Verdict:
    """llama.cpp (recent builds): GET /props -> modalities.vision.

    Some server deployments put /props at the server root rather than
    under /v1 (ninfer does; llama.cpp itself serves it at root). Re-base
    to the root when the api_base ends in /v1 - the same trick the Ollama
    rule uses - so the props endpoint is found regardless of the trailing
    suffix the provider config carries.
    """
    root = api_base.rstrip("/")
    for suffix in ("/v1", ""):
        if root.endswith(suffix) and suffix:
            root = root[:-len(suffix)]
    props = await _get_json(root, "/props", api_key)
    if not isinstance(props, dict):
        return None
    modalities = props.get("modalities")
    if isinstance(modalities, dict):
        v = modalities.get("vision")
        if v is True:
            return True
        if v is False:
            return False
    return None


def _args_have_mmproj(args: list) -> Optional[bool]:
    """Check launch argv for vision projector indicators.

    Returns True if a projector flag is found. Returns None if no
    projector flag is found - the caller decides whether that means
    False (llama.cpp: absence of --mmproj is definitive) or passes
    through as unknown (other server types where vision may be baked
    into the artifact).
    """
    if not isinstance(args, list):
        return None
    # llama.cpp uses --mmproj; ninfer uses --vision; some use --vision-projector
    flags = ("--mmproj", "--vision", "--vision-projector")
    for f in flags:
        if f in args:
            return True
    return None


async def _rule_llamacpp_models_args(api_base: str, model: str, api_key: str) -> Verdict:
    """Server /v1/models entries embed the instance's launch argv under
    status.args - projector flags present/absent are definitive per model id.

    For llama.cpp: absence of --mmproj is a definitive False.
    For other server types (e.g. ninfer): presence of --vision or similar
    is True; absence is None (unknown), not False.
    """
    data = await _get_json(api_base, "/v1/models", api_key)
    if not isinstance(data, dict):
        return None
    for m in data.get("data") or []:
        if not isinstance(m, dict) or str(m.get("id") or "") != model:
            continue
        has_proj = _args_have_mmproj((m.get("status") or {}).get("args"))
        if has_proj is True:
            return True
        # Absence of projector flag: definitive False only for llama.cpp.
        # Other server types may bake vision into the artifact and use
        # no projector flag at all - abstain (return None = unknown).
        if _looks_like_llamacpp_argv((m.get("status") or {}).get("args")):
            return False
    return None


def _looks_like_llamacpp_argv(args: list) -> bool:
    """Heuristic: argv contains llama.cpp-specific flags."""
    if not isinstance(args, list):
        return False
    # The executable name or llama.cpp-specific flags
    llama_flags = ("server", "-m", "--model", "-n", "--n-predict", "-t", "--threads",
                  "-c", "--ctx-size", "-ngl", "--n-gpu-layers")
    for arg in args:
        if arg == "llama-server":
            return True
        if arg in llama_flags:
            return True
    return False


async def _rule_openrouter_catalog(api_base: str, model: str, api_key: str) -> Verdict:
    """OpenRouter: architecture.input_modalities from the models catalog.
    The catalog is already fetched for the Settings model dropdown; the
    single-flight lock keeps one GET per TTL window when several models
    ask concurrently. The data is curated: an entry whose modalities lack
    "image" is a definitive False, not unknown."""
    if "openrouter" not in api_base.lower():
        return None
    async with _catalog():
        data = await _get_json(api_base, "/models", api_key)
    if not isinstance(data, dict):
        return None
    for m in data.get("data") or []:
        if not isinstance(m, dict) or str(m.get("id") or "") != model:
            continue
        arch = m.get("architecture") or {}
        mods = arch.get("input_modalities")
        if isinstance(mods, list):
            return "image" in [str(x).lower() for x in mods]
        return None
    return None


async def _rule_ollama_show(api_base: str, model: str, api_key: str) -> Verdict:
    """Ollama: POST /api/show -> capabilities list containing 'vision'.
    Needs the native (non-/v1) root, so only tried when the base hints at
    Ollama - an OpenAI-compatible /v1 base would just 404 anyway, but the
    hint keeps the noise down."""
    if ":11434" not in api_base and "ollama" not in api_base.lower():
        return None
    root = api_base.rstrip("/")
    for suffix in ("/v1", ""):
        if root.endswith(suffix) and suffix:
            root = root[: -len(suffix)]
    url = root + "/api/show"
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            r = await client.post(url, json={"name": model})
        if r.status_code != 200:
            return None
        body = r.json()
    except Exception:  # noqa: BLE001
        return None
    caps = body.get("capabilities")
    if isinstance(caps, list):
        return "vision" in [str(c).lower() for c in caps]
    return None


_RULES: list[Callable[[str, str, str], object]] = [
    _rule_llamacpp_props,
    _rule_llamacpp_models_args,
    _rule_openrouter_catalog,
    _rule_ollama_show,
]


# --------------------------------------------------------------- public API

async def vision_capability(api_base: str, model: str, api_key: str = "") -> Verdict:
    """True / False (definitive, from a probe rule or recorded evidence) or
    None (unknown - behave exactly like the pre-#430 world)."""
    key = (api_base.rstrip("/"), model)
    hit = _CACHE.get(key)
    if hit is not None and not hit.expired():
        return hit.verdict
    if hit is not None and hit.expired():
        _CACHE.pop(key, None)

    cfg = _cfg()
    resolved_key, request_model = _lookup(cfg, api_base, model)
    effective_key = api_key or resolved_key

    for rule in _RULES:
        try:
            v = await rule(api_base, request_model, effective_key)
        except Exception:  # noqa: BLE001 - a rule crash is an unknown
            v = None
        if v is True or v is False:
            _CACHE[key] = _Entry(v, _TTL, getattr(rule, "__name__", "rule"))
            return v

    _CACHE[key] = _Entry(None, _NEG_TTL, "unknown")
    return None


def record_rejection(api_base: str, model: str) -> None:
    """#428's reactive handler reporting hard evidence: this provider
    rejected image input. Cached as False (deterministic ground truth),
    overriding any probe verdict for the TTL."""
    if not api_base:
        return
    _CACHE[(api_base.rstrip("/"), model)] = _Entry(False, _REJECT_TTL, "rejection")


def record_unknown(api_base: str, model: str) -> None:
    """A reactive rejection on an 'unknown' provider was downgraded to a
    plain ModelError (the strip retry already ran): nothing to learn.
    Kept as a seam for symmetry/debugging."""
    return None


async def capability_capped(
    api_base: str,
    model: str,
    api_key: str = "",
) -> Verdict:
    """The turn-seam entry point: vision_capability under a hard time cap.
    A capped wait is NOT a blocking probe - a slow or dead server yields
    None and the turn proceeds (the reactive path stays the backstop)."""
    try:
        return await asyncio.wait_for(
            vision_capability(api_base, model, api_key), timeout=CAP_BUDGET
        )
    except Exception:  # noqa: BLE001 - includes TimeoutError
        return None


async def preflight(api_base: str, model: str, api_key: str = "") -> None:
    """Fire-and-forget prefetch (spawned at the image-attachment seam):
    warm the cache before the turn's seam asks. Never raises, never
    blocks anything."""
    try:
        await vision_capability(api_base, model, api_key)
    except Exception:  # noqa: BLE001
        pass


def cache_snapshot() -> dict:
    """Test/debug view: {(api_base, model): {verdict, source, age}}."""
    out = {}
    for key, e in list(_CACHE.items()):
        out[repr(key)] = {
            "verdict": e.verdict,
            "source": e.source,
            "age": round(time.monotonic() - e.at, 1),
        }
    return out


def _reset_cache_for_tests() -> None:
    _CACHE.clear()


async def probe_now(api_base: str, model: str, api_key: str = "") -> Verdict:
    """Bypass the cache entirely and run the rules once. Test hook."""
    key = (api_base.rstrip("/"), model)
    cfg = _cfg()
    resolved_key, request_model = _lookup(cfg, api_base, model)
    effective_key = api_key or resolved_key
    for rule in _RULES:
        try:
            v = await rule(api_base, request_model, effective_key)
        except Exception:  # noqa: BLE001
            v = None
        if v is True or v is False:
            _CACHE[key] = _Entry(v, _TTL, getattr(rule, "__name__", "rule"))
            return v
    _CACHE[key] = _Entry(None, _NEG_TTL, "unknown")
    return None

# ponytail: This fix targets a specific bug (vision-capable ninfer
# servers with /v1 api_bases getting False negatives) with minimal change.
# The /v1-stripping heuristic in _rule_llamacpp_props handles the observed
# root cause; if a server has a different props path scheme, the rule
# returns None (unknown) and the probe falls through to later rules —
# it does not produce a wrong False. The reactive #428 retry remains the
# backstop for any case the probe misclassifies.
