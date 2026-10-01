"""Issue #197 - tool-description and lazy-tier doc corrections.

Pins the wording fixes from the prompt-surface review (SYN-33/34/35/37/38/
39/40/41/42): every documented string must match executor behavior.
"""

from backend.agent.computer import COMPUTER_HELP_DOCS, COMPUTER_TOOLS_SCHEMA
from backend.agent.tools import (
    HELP_DOCS,
    INSTALL_GIT_SCHEMA,
    POWERSHELL_SCHEMA,
    TOOLS_SCHEMA,
)


def _schema_text() -> str:
    import json

    return json.dumps(TOOLS_SCHEMA + [POWERSHELL_SCHEMA, INSTALL_GIT_SCHEMA])


# SYN-35: per-call env construction means no restart is ever needed.
def test_install_git_no_restart_claim():
    text = INSTALL_GIT_SCHEMA["function"]["description"]
    assert "restart" not in text.lower()
    assert "per call" in text or "picked up" in text.lower()


# SYN-41: a bare "429" never surfaces to the model; teach observable signs.
def test_web_search_help_no_429():
    text = HELP_DOCS["web_search"]
    assert "429" not in text
    assert "challenge" in text or "empty" in text.lower()


# SYN-40: the executor itself retries across three routes; wording must not
# forbid a single delayed retry.
def test_web_fetch_retry_wording():
    schema = next(
        s for s in TOOLS_SCHEMA if s["function"]["name"] == "web_fetch"
    )["function"]["description"]
    for text in (schema, HELP_DOCS["web_fetch"]):
        assert "instead of retrying" not in text
        assert "hammer" in text  # the real guidance: don't hammer, retry once


# SYN-42: IMAGES ON PAGE is capped at 5 URLs - documented now.
def test_web_fetch_images_cap_documented():
    schema = next(
        s for s in TOOLS_SCHEMA if s["function"]["name"] == "web_fetch"
    )["function"]["description"]
    assert "first 5" in schema or "first 5" in HELP_DOCS["web_fetch"]


# SYN-38: a 5th parallel call queues on a semaphore; it is not rejected.
def test_spawn_agent_queue_semantics():
    schema = next(
        s for s in TOOLS_SCHEMA if s["function"]["name"] == "spawn_agent"
    )["function"]["description"]
    assert "max 4 at once" in schema
    assert "queue" in schema
    assert "queue" in HELP_DOCS["spawn_agent"]


# SYN-39: unknown type values are silently coerced to "project".
def test_memory_save_type_coercion_documented():
    schema = next(
        s for s in TOOLS_SCHEMA if s["function"]["name"] == "memory_save"
    )["function"]
    type_desc = schema["parameters"]["properties"]["type"]["description"]
    assert "coerc" in type_desc.lower()


# SYN-37: the never-shell-background rationale lives in ONE shared sentence
# (duplicated across bash + powershell schemas at baseline) and reflects
# kill-tree behavior (children are killed, not wedged forever).
def test_background_warning_deduplicated_and_current():
    text = _schema_text()
    assert text.count("outlives the tool call") == 0
    assert text.count("killed at timeout, losing their work") == 2


# SYN-33: lazy-tier entries must not verbatim-copy their short schemas.
def test_lazy_tier_not_verbatim_copy_of_schema():
    schema_by_name = {
        s["function"]["name"]: s["function"]["description"]
        for s in COMPUTER_TOOLS_SCHEMA
    }
    for name in ("read_ui_tree", "mouse_drag"):
        note = COMPUTER_HELP_DOCS[name]
        schema = schema_by_name[name]
        # No single sentence (>=60 chars) may appear in both verbatim.
        schema_sents = {s.strip() for s in schema.split(". ") if len(s.strip()) >= 60}
        for sent in (s.strip() for s in note.split(". ")):
            assert sent not in schema_sents, f"{name}: lazy note copies schema: {sent}"
