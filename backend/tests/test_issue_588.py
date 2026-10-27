"""#588: per-model compaction entries must be reachable and able to bite.

Three stacked faults from the issue:
A. entries saved under one router-slot alias were invisible to a chat
   running a sibling alias (exact-id lookup only);
B. the miss was silent (80%-of-window fallback, no readout);
C. the cut math kept 40% of the WINDOW regardless of the trigger, so a
   24k absolute trigger could not fold until ~3x past itself.
"""
import json

import pytest
from fastapi.testclient import TestClient

from backend.agent import compaction as comp
from backend.agent import model_settings as ms
from backend.db import database as db


def _msg(role, text):
    return {"role": role, "content": text}


# The live #1092 shape: the provider's DEFAULT model (and so the entry
# Settings would key) carried the "1." router-slot prefix while the chat
# ran the same model under the "x2." slot prefix. Same base model after
# the prefix strip -- different bare ids, which is what broke the exact
# lookup. (x2.qwen36-reap50 vs x2.Tiel are DIFFERENT models sharing a
# slot prefix; aliasing must never bridge those.)
ENTRY_KEY = "1.Tiel-Coder-35B-A3B-MTP-UD-Q4_K_S"
CHAT_MODEL = "x2.Tiel-Coder-35B-A3B-MTP-UD-Q4_K_S"


def _cfg588(**over):
    cfg = {
        "enabled": True,
        "trigger_tokens": 0,
        "per_model_enabled": True,
        "per_model_trigger_tokens": 300_000,
        "model": "",
        "model_compaction": {
            ENTRY_KEY: {"enabled": True, "trigger_tokens": 24_000},
        },
        **over,
    }
    return cfg


def _patch_cfg(monkeypatch, cfg):
    monkeypatch.setattr(comp, "_compaction_cfg", lambda: cfg)


@pytest.fixture(autouse=True)
def _fake_summarizer(monkeypatch):
    """Deterministic summarizer (same contract as test_compaction.py)."""
    async def fake_summarize(cut_messages, model_override="", cancel_event=None):
        return f"SYNTHETIC SUMMARY of {len(cut_messages)} messages"

    monkeypatch.setattr(comp, "summarize_messages", fake_summarize)


# ------------------------------------------------- identity (fault A)


def test_family_base_strips_router_slot_prefixes():
    # Both slot prefixes of the SAME model collapse to one base.
    assert ms.family_base_id("1.Tiel-Coder-35B-A3B-MTP-UD-Q4_K_S") == (
        "Tiel-Coder-35B-A3B-MTP-UD-Q4_K_S"
    )
    assert ms.family_base_id(CHAT_MODEL) == "Tiel-Coder-35B-A3B-MTP-UD-Q4_K_S"
    # Different models sharing a slot prefix do NOT collapse together.
    assert ms.family_base_id("x2.qwen36-reap50-Q4_K_M-imat") == "qwen36-reap50-Q4_K_M-imat"
    assert ms.family_base_id(ENTRY_KEY) != ms.family_base_id("x2.qwen36-reap50-Q4_K_M-imat")


def test_family_base_leaves_cloud_and_dotted_ids_alone():
    assert ms.family_base_id("openrouter/google/gemini-2.5-pro") == (
        "openrouter/google/gemini-2.5-pro"
    )
    assert ms.family_base_id("gpt-4o") == "gpt-4o"
    assert ms.family_base_id("~google/gemini-flash-latest") == "~google/gemini-flash-latest"
    # A prefixed id with no known family stem stays intact.
    assert ms.family_base_id("x2.custom-thing") == "x2.custom-thing"


def test_bare_model_id_scopes():
    assert ms.bare_model_id("llamacpp::" + CHAT_MODEL) == CHAT_MODEL
    assert ms.bare_model_id(CHAT_MODEL) == CHAT_MODEL
    assert ms.bare_model_id("") == ""


def test_entry_key_resolution_order():
    cfg = _cfg588(model_compaction={
        ENTRY_KEY: {"enabled": True, "trigger_tokens": 24_000},
        "Tiel-Coder-35B-A3B-MTP-UD-Q4_K_S": {"enabled": True, "trigger_tokens": 99_000},
    })
    # Exact key beats the family base.
    assert ms.model_entry_key(CHAT_MODEL, cfg) == "Tiel-Coder-35B-A3B-MTP-UD-Q4_K_S"
    # Sibling alias resolves to the entry saved under the other prefix.
    cfg2 = _cfg588()
    assert ms.model_entry_key(CHAT_MODEL, cfg2) == ENTRY_KEY
    # No entry anywhere.
    assert ms.model_entry_key("gpt-4o", cfg2) is None


def test_configured_model_ids_lists_saved_entries():
    assert ms.configured_model_ids(_cfg588()) == [ENTRY_KEY]
    assert ms.configured_model_ids({"model_compaction": {"junk": "not-a-dict"}}) == []


# --------------------------------------- runtime consumption (fault A)


def test_per_model_cfg_resolves_alias_entry(monkeypatch):
    _patch_cfg(monkeypatch, _cfg588())
    r = comp._compaction_cfg_for_model(CHAT_MODEL, comp._compaction_cfg(), 131_072)
    # The chat's id has NO exact entry; the sibling alias's 24k applies.
    assert r["trigger_tokens"] == 24_000
    assert r["enabled"] is True


def test_should_compact_fires_on_alias_trigger(monkeypatch):
    _patch_cfg(monkeypatch, _cfg588())
    # The incident's measured 31.4k vs the 24k trigger, window 131k: the
    # old exact-id lookup derived ~104.9k and never fired.
    assert comp.should_compact(31_395, 131_072, CHAT_MODEL) is True
    assert comp.should_compact(23_000, 131_072, CHAT_MODEL) is False


# ------------------------------------------ keep budget (fault C)


def test_keep_budget_derives_from_absolute_trigger(monkeypatch):
    _patch_cfg(monkeypatch, _cfg588())
    ccfg = comp._compaction_cfg_for_model(CHAT_MODEL, comp._compaction_cfg(), 131_072)
    # 24k trigger in force: the cut keeps 40% of the TRIGGER (9.6k), not
    # 40% of the 131k window (52.4k) which no 24k transcript could fold.
    assert comp.keep_budget_tokens(ccfg, 131_072) == 9_600


def test_keep_budget_ship_shape_without_trigger(monkeypatch):
    # No entry: the trigger derives from the window (80% = 104,857), and
    # the keep folds to 40% OF THAT TRIGGER (min(trigger, window)) --
    # folding to 40% of what actually fired, not of the bigger window.
    _patch_cfg(monkeypatch, _cfg588(model_compaction={}))
    ccfg = comp._compaction_cfg_for_model(CHAT_MODEL, comp._compaction_cfg(), 131_072)
    assert ccfg["trigger_tokens"] == 104_857  # window-derived fallback
    assert comp.keep_budget_tokens(ccfg, 131_072) == int(104_857 * 0.40)

    # The shipped 300k default trigger sits ABOVE the window: the window
    # fraction is the smaller keep, so the ship shape holds.
    _patch_cfg(monkeypatch, _cfg588(model_compaction={}, trigger_tokens=300_000))
    ccfg = comp._compaction_cfg_for_model(CHAT_MODEL, comp._compaction_cfg(), 131_072)
    assert ccfg["trigger_tokens"] == 300_000
    assert comp.keep_budget_tokens(ccfg, 131_072) == int(131_072 * 0.40)


def test_keep_budget_disabled_config_keeps_window_fraction(monkeypatch):
    _patch_cfg(monkeypatch, _cfg588(model_compaction={
        ENTRY_KEY: {"enabled": False, "trigger_tokens": 24_000},
    }))
    ccfg = comp._compaction_cfg_for_model(CHAT_MODEL, comp._compaction_cfg(), 131_072)
    assert ccfg["enabled"] is False
    assert comp.keep_budget_tokens(ccfg, 131_072) == int(131_072 * 0.40)


# ------------------------------------------------------- e2e (A + C)


@pytest.mark.asyncio
async def test_alias_entry_compacts_the_incident_transcript(monkeypatch):
    """The live #1092 shape, end to end: the 24k entry saved under the
    "1."-prefixed default id, the chat running the SAME model under its
    "x2." slot id, 31.4k measured on a 131k window, and a transcript
    whose est size (~24k) sits ABOVE the derived 9.6k keep (a cut
    exists) but far BELOW the old ship 52k keep (no cut was allowed).
    The old code returned None (trigger miss); the fixed code folds."""
    _patch_cfg(monkeypatch, _cfg588())

    async def fake_resolve_window(model, cfg=None):
        return 131_072

    monkeypatch.setattr(comp, "resolve_window", fake_resolve_window)
    cid = await db.create_conversation("incident")
    # 40 rows x ~2400 chars = ~24k est-tokens: too big for the derived
    # 9.6k keep (a cut exists), far too small for the old 52k keep.
    for i in range(20):
        await db.add_message(cid, "user", ("u" + str(i)) * 1200)
        await db.add_message(cid, "assistant", ("a" + str(i)) * 1200)
    await db.set_conversation_usage(cid, 31_395, CHAT_MODEL)

    result = await comp.compact_history_for_context(cid, model_id=CHAT_MODEL)
    assert result and result["type"] == "compacted"
    assert result["summarized_messages"] > 0
    rows = await db.get_messages(cid)
    assert len(rows) == 40  # transcript untouched (adr/0004)

    # And the trigger decision behind it, spelled out:
    ccfg = comp._compaction_cfg_for_model(CHAT_MODEL, comp._compaction_cfg(), 131_072)
    assert ccfg["trigger_tokens"] == 24_000
    assert comp.keep_budget_tokens(ccfg, 131_072) == 9_600


@pytest.mark.asyncio
async def test_absolute_trigger_lets_the_cut_bite_on_exact_key(monkeypatch):
    """Fault C isolated: even a matching exact entry could not fold below
    ~3x its trigger while the keep budget answered to the window. With
    the trigger-derived budget, a 24k-triggered transcript folds."""
    _patch_cfg(monkeypatch, _cfg588(model_compaction={
        CHAT_MODEL: {"enabled": True, "trigger_tokens": 24_000},
    }))

    async def fake_resolve_window(model, cfg=None):
        return 131_072

    monkeypatch.setattr(comp, "resolve_window", fake_resolve_window)
    cid = await db.create_conversation("exact-key")
    for i in range(20):
        await db.add_message(cid, "user", ("u" + str(i)) * 1200)
        await db.add_message(cid, "assistant", ("a" + str(i)) * 1200)
    await db.set_conversation_usage(cid, 31_395, CHAT_MODEL)
    result = await comp.compact_history_for_context(cid, model_id=CHAT_MODEL)
    assert result and result["type"] == "compacted"


# ------------------------------------------------------------ API (B)


def _write_cfg(tmp_path, cfg):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def test_effective_endpoint_reports_entry_and_keep(tmp_path, monkeypatch):
    from backend.agent import config as cfgmod

    _write_cfg(tmp_path, {
        "access_mode": "full",
        "model_compaction": {ENTRY_KEY: {"enabled": True, "trigger_tokens": 24_000}},
    })
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    import backend.main as mainmod

    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    from backend.main import app

    with TestClient(app) as client:
        # Alias sibling: the endpoint names the covering entry and the
        # trigger-derived keep budget.
        r = client.get("/api/compaction/effective", params={"model": CHAT_MODEL})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["entry"] is True
        assert body["entry_key"] == ENTRY_KEY
        assert body["enabled"] is True
        assert body["trigger_tokens"] == 24_000
        assert body["keep_tokens"] == 9_600

        # No entry: the fallback is reported as what it is (fault B).
        r = client.get("/api/compaction/effective", params={"model": "gpt-4o"})
        body = r.json()
        assert body["entry"] is False
        assert body["entry_key"] is None
        assert body["trigger_tokens"] > 0  # derived, shown as such

        # Empty model: the global shape, entry false.
        r = client.get("/api/compaction/effective")
        body = r.json()
        assert body["model"] == ""
        assert body["entry"] is False
