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
import sys
from pathlib import Path


def _default_config_dir() -> Path:
    """Mirrors database._default_data_dir: the packaged app is a PyInstaller
    --onefile bundle, so __file__ points into a throwaway temp extraction
    that vanishes on exit — config written there never persisted."""
    if getattr(sys, "frozen", False):
        home = Path.home() / ".yaah"
        home.mkdir(parents=True, exist_ok=True)
        return home
    return Path(__file__).parent.parent / "data"


# YAAH_CONFIG_PATH lets the test suite redirect this (default path is the
# user's real config).
CONFIG_PATH = Path(
    os.environ.get("YAAH_CONFIG_PATH")
    or _default_config_dir() / "config.json"
)

DEFAULTS = {
    "temperature": 0.2,
    "max_tokens": 0,
    # Agent loop tool-call rounds per turn (Settings → Max steps).
    "max_steps": 200,
    # Last workspace chosen in the sidebar, so it survives app restarts.
    "last_workspace": "",
    # Voice dictation: local whisper.cpp by default; cloud = BYOK
    # OpenAI-compatible /audio/transcriptions endpoint.
    "voice": {
        "engine": "local",
        "cloud_endpoint": "",
        "cloud_api_key": "",
        "cloud_model": "whisper-1",
    },
    # LAN hosting (see backend/agent/remote.py + discovery.py). Hosting is
    # on by default; a host with no passphrase refuses remote exec.
    "remote": {
        "hosting_enabled": True,
        "passphrase": "",
        "display_name": "",
    },
}

DEFAULT_PROVIDER = {
    "api_base": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o",
}


def _migrate(raw: dict) -> dict:
    """Synthesize a providers map from a legacy flat config (only when
    legacy fields actually exist — a fresh install starts with NO
    providers and the user configures the first one)."""
    if isinstance(raw.get("providers"), dict) and raw["providers"]:
        return raw
    if not any(k in raw for k in ("api_base", "api_key", "model")):
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
    cfg.setdefault("providers", {})
    cfg.setdefault("active_provider", "")

    providers = {
        name: {**DEFAULT_PROVIDER, **(p or {})} for name, p in cfg["providers"].items()
    }
    active = cfg.get("active_provider")
    if active not in providers:
        # Empty map = fresh install, nothing configured yet.
        active = next(iter(providers), "")
    cfg["providers"] = providers
    cfg["active_provider"] = active

    # Environment overrides everything (active provider only)
    ap = providers.get(active) if active else None
    if ap is None:
        # Derived single-provider view stays blank; model_client turns a
        # chat attempt into a clear "configure a provider" error.
        cfg.update({"api_base": "", "api_key": "", "model": ""})
        return cfg
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


def set_last_workspace(workspace: str):
    """Remember the workspace the UI last used (Q: persist across restarts)."""
    ws = (workspace or "").strip()
    if not ws or ws == ".":
        return
    save_config({"last_workspace": ws})


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
    current.setdefault("providers", {})
    # An empty providers map is legitimate (fresh install, user hasn't
    # configured anything yet) — persist it as-is rather than re-seeding a
    # placeholder provider that 401s out of the box.
    if current.get("active_provider") not in current.get("providers", {}):
        current["active_provider"] = next(iter(current.get("providers", {})), "")
    # Keep the file in the new shape only
    for k in ("api_base", "api_key", "model"):
        current.pop(k, None)
    CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
