"""Curated skill-repo ledger (#57, Phase 3 seed).

A small, hand-curated JSON list of audited skill repositories with a
compatibility verdict, so the future skill manager's install panel can
show a trustworthy verdict for popular repos instead of relying on
heuristics alone. Verdicts follow the taxonomy from issue #57:

  A — drop-in: copy the folder, it works.
  B — works with caveats: installs, but the body assumes something
      YAAH must have or the user must provide.
  C — harness-coupled: needs a runtime feature YAAH doesn't have
      (subagents, plugin host, build step); a port, not an install.

Hand-made/local skills simply aren't in the ledger; the ledger never
overrides what is actually on disk — it only annotates install-time
sources.
"""
import json
from pathlib import Path

_LEDGER_PATH = Path(__file__).parent.parent / "bundled_skills" / "skills-ledger.json"

_VALID_VERDICTS = ("A", "B", "C")
_REQUIRED_KEYS = ("repo", "verdict", "title", "notes")


def load_ledger(path: Path | None = None) -> list[dict]:
    """Load and validate ledger entries from ``path`` (default: the
    bundled seed). Bad entries are skipped, never raised — a malformed
    ledger must not break skill scanning or the manager UI."""
    p = Path(path) if path else _LEDGER_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if any(not isinstance(item.get(k), str) or not item.get(k)
               for k in _REQUIRED_KEYS):
            continue
        if item["verdict"] not in _VALID_VERDICTS:
            continue
        out.append({k: item[k] for k in _REQUIRED_KEYS})
    return out


def load_bundled_ledger() -> list[dict]:
    """The ledger shipped with the app (the 10 repos audited in #57)."""
    return load_ledger()


def lookup(repo: str) -> dict | None:
    """Ledger entry for ``owner/name``, or None."""
    for entry in load_bundled_ledger():
        if entry["repo"] == repo:
            return entry
    return None


def verdict_counts() -> dict[str, int]:
    counts = {"A": 0, "B": 0, "C": 0}
    for entry in load_bundled_ledger():
        counts[entry["verdict"]] += 1
    return counts
