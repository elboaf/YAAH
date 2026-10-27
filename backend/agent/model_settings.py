"""Model-id identity for per-model settings.

Per-model settings (config.model_compaction today) key on the BARE model
id a chat runs on. The Settings editor cannot know every id a chat may
pick (router catalogs rotate slot-prefixed aliases of the same base
model), so an exact-key-only lookup makes saved entries unreachable
(#588 fault A). This module owns the identity rules:

- `bare_model_id`: strip the "provider::" scope.
- `family_base_id`: the model FAMILY base — router-slot prefixes
  ("1.Tiel-...", "x2.Tiel-...") collapse to one base so an entry saved
  for one alias covers its siblings.
- `model_entry`: the saved per-model entry for an id — exact key first,
  then the family base, then any configured alias of the same family.
- `configured_model_ids`: every id with a saved entry (Settings renders
  one editor entry per saved id, not per default model).

Identity is a property of the id STRING, not of any provider probe:
no network, no config writes, cheap enough for the per-call path.
"""
from __future__ import annotations

import re

# Local-model family stems (lowercase prefix match) that router-slot
# prefixes ride in front of. Built from the llama.cpp router catalogs
# this app actually serves (#588): a prefix is stripped only when what
# follows starts with one of these stems, so ordinary namespaced cloud
# ids ("openrouter/gemini-...", "z-ai/glm-...") and dotted product
# names are never mangled.
_FAMILY_STEMS = (
    "tiel",
    "qwen",
    "llama",
    "mistral",
    "phi",
    "gemma",
    "deepseek",
    "glm",
    "kimi",
    "codestral",
    "mixtral",
    "smollm",
    "falcon",
    "starcoder",
    "wizardcoder",
)

# A router-slot prefix: one or two dot-separated labels in front of the
# family stem, digits or short lower-case tokens ("x2.", "1.", "2.q").
_SLOT_PREFIX = re.compile(r"^(?:(?:[a-z]|[a-z]?\d){1,4})\.")


def bare_model_id(model: str | None) -> str:
    """The bare model id: "provider::model" reduces to `model`; anything
    else passes through unchanged (already bare)."""
    if not model:
        return ""
    return model.partition("::")[2] if "::" in model else model


def _has_family_stem(rest: str) -> bool:
    r = rest.lower()
    return any(r.startswith(stem) for stem in _FAMILY_STEMS)


def family_base_id(model: str | None) -> str:
    """Family base for a bare model id: router-slot prefixes collapse so
    an entry saved for one alias covers its siblings ("1.Tiel-Coder-..."
    and "x2.Tiel-Coder-..." share `Tiel-Coder-...`).

    A prefix is stripped only when what follows starts with a known
    local-model family stem; anything else (namespaced cloud ids, dotted
    product names) passes through unchanged.
    """
    bare = bare_model_id(model)
    m = _SLOT_PREFIX.match(bare)
    if m and _has_family_stem(bare[m.end():]):
        return bare[m.end():]
    return bare


def configured_model_ids(cfg: dict) -> list[str]:
    """Every model id with a saved per-model compaction entry
    (config.model_compaction keys), in config order."""
    mc = cfg.get("model_compaction")
    if not isinstance(mc, dict):
        return []
    return [str(k) for k, v in mc.items() if isinstance(v, dict)]


def model_entry_key(model: str | None, cfg: dict) -> str | None:
    """The config key whose entry covers a model id, or None when the
    model has no entry.

    Resolution order (first hit wins):
    1. the exact bare id;
    2. the family base id (an entry saved under the de-prefixed form);
    3. any configured id of the same family base (an entry saved under a
       sibling router-slot alias).
    """
    bare = bare_model_id(model)
    if not bare:
        return None
    mc = cfg.get("model_compaction")
    if not isinstance(mc, dict):
        return None
    if isinstance(mc.get(bare), dict):
        return bare
    base = family_base_id(bare)
    if isinstance(mc.get(base), dict):
        return base
    for key, value in mc.items():
        if isinstance(value, dict) and family_base_id(str(key)) == base:
            return str(key)
    return None


def model_entry(model: str | None, cfg: dict) -> dict | None:
    """The saved per-model compaction entry for a model id, or None when
    the model has no entry (see model_entry_key for the resolution
    order)."""
    key = model_entry_key(model, cfg)
    if key is None:
        return None
    entry = (cfg.get("model_compaction") or {}).get(key)
    return entry if isinstance(entry, dict) else None
