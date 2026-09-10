"""Configuration: model provider settings.

Reads from environment variables with sane defaults. Users can point this at
any OpenAI-compatible endpoint (OpenAI, OpenRouter, Ollama, LM Studio...).
"""
import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "data" / "config.json"

DEFAULTS = {
    "api_base": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o",
    "temperature": 0.2,
    "max_tokens": 0,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    # Environment overrides everything
    cfg["api_base"] = os.environ.get("AGENT_API_BASE", cfg["api_base"])
    cfg["api_key"] = os.environ.get("AGENT_API_KEY", cfg["api_key"])
    cfg["model"] = os.environ.get("AGENT_MODEL", cfg["model"])
    return cfg


def save_config(updates: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    if CONFIG_PATH.exists():
        try:
            current = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    current.update(updates)
    # Never persist an empty key over an existing one
    if not current.get("api_key"):
        current.pop("api_key", None)
    CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")