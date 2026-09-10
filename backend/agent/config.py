"""Configuration: model provider settings.

Multi-provider config: ``config.json`` holds a ``providers`` map
(name -> {api_base, api_key, model}) plus the ``active_provider`` whose
entry the agent chats through. Switching providers happens purely by
selecting a model from a provider's group in the sidebar dropdown.

Legacy flat configs (single api_base/api_key/model) are migrated on load.
Environment variables (AGENT_API_BASE/KEY/MODEL) override the active
provider's fields.
"""
import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "data" / "config.json"

DEFAULTS = {
    "temperature": 0.2,
    "max_tokens": 0,
}

DEFAULT_PROVIDER = {
    "api_base": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o",
}


def _migrate(raw: dict) -> dict:
    """Synthesize a providers map from a legacy flat config."""
    if isinstance(raw.get("providers"), dict) and raw["providers"]:
        return raw
    providers = {
        "openai": {
            "api_base": raw.get("api_base") or DEFAULT_PROVIDER["api_base"],
            "api_key": raw.get("api_key") or "",
            "model": raw.get("model") or DEFAULT_PROVIDER["model"],
        }
    }
    raw["providers"] = providers
    raw["active_provider"] = "openai"
    # Drop the flat fields so they never shadow the merged view
    for k in ("api_base", "api_key", "model"):
        raw.pop(k, None)
    return raw


def load_config() -> dict:
    """Merged config: providers map + top-level fields derived from the
    active provider (api_base/api_key/model), so existing callers that read
    cfg['api_base'] etc. keep working unchanged."""
    cfg = dict(DEFAULTS)
    raw: dict = {}
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}
    raw = _migrate(raw or {})
    cfg.update({k: v for k, v in raw.items() if k not in ("api_base", "api_key", "model")})

    providers = {
        name: {**DEFAULT_PROVIDER, **(p or {})} for name, p in cfg["providers"].items()
    }
    active = cfg.get("active_provider")
    if active not in providers:
        active = next(iter(providers))
    cfg["providers"] = providers
    cfg["active_provider"] = active

    # Environment overrides everything (active provider only)
    ap = providers[active]
    ap["api_base"] = os.environ.get("AGENT_API_BASE", ap["api_base"])
    ap["api_key"] = os.environ.get("AGENT_API_KEY", ap["api_key"])
    ap["model"] = os.environ.get("AGENT_MODEL", ap["model"])

    # Derived top-level view for callers that expect a single provider
    cfg["api_base"] = ap["api_base"]
    cfg["api_key"] = ap["api_key"]
    cfg["model"] = ap["model"]
    return cfg


def set_active_model(provider: str, model: str):
    """Select a model from a provider's group: makes that provider active and
    remembers the model it was last used with (Q6)."""
    cfg = load_config()
    if provider not in cfg["providers"]:
        return
    save_config({"active_provider": provider, "providers": {
        **{n: p for n, p in cfg["providers"].items() if n != provider},
        provider: {**cfg["providers"][provider], "model": model},
    }})


def save_config(updates: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    current: dict = {}
    if CONFIG_PATH.exists():
        try:
            current = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            current = {}
    current = _migrate(current or {})

    incoming_providers = updates.get("providers")
    if isinstance(incoming_providers, dict):
        # The incoming map is authoritative for membership: a provider absent
        # from it was deleted in the UI and must not be resurrected here.
        # Merge keys only for providers still present — an absent/blank key
        # never wipes a saved one.
        saved = current.get("providers", {})
        merged = {}
        for name, p in incoming_providers.items():
            base = saved.get(name, {})
            new = {**base, **{k: v for k, v in (p or {}).items() if v not in (None, "")}}
            # explicit removal of a key is not supported via merge; blank keeps old
            merged[name] = new
        current["providers"] = merged
        updates = {k: v for k, v in updates.items() if k != "providers"}

    current.update(updates)
    # Never persist an empty active provider
    if not current.get("providers"):
        current["providers"] = {"openai": dict(DEFAULT_PROVIDER)}
        current["active_provider"] = "openai"
    if current.get("active_provider") not in current["providers"]:
        current["active_provider"] = next(iter(current["providers"]))
    # Keep the file in the new shape only
    for k in ("api_base", "api_key", "model"):
        current.pop(k, None)
    CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
