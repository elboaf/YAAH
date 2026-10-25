"""Issue #557: slot-cache pure decision core + wire-through contracts.

No network: every server touch is faked. The live-API behavior itself was
verified against the real router during fact-checking (see the issue).
"""

import asyncio
import json

import pytest

from backend.agent import slotcache as sc


# ---------------------------------------------------------------------------
# Pure decision core
# ---------------------------------------------------------------------------


def _led(*bins):
    return {"bins": list(bins)}


TURN1 = {
    "file": "conv-42-t1.bin",
    "kind": "turn",
    "conversation_id": 42,
    "turn": 1,
    "n_tokens": 100,
    "n_written": 1000,
    "boot": "bootA",
    "id_task_at_save": 7,
    "prompt_key": "sys-x.bin",
    "ts": 1,
}


def test_gate_skips_when_no_slot_info():
    d = sc.decide(None, None, "bootA", _led(), 42, False, "sys-x.bin")
    assert d == {"action": "skip", "bin": None, "reason": "no-slot-info"}


def test_hot_when_boot_id_task_and_conversation_match():
    recorded = {
        "boot": "bootA", "id_task": 9, "conversation_id": 42,
        "prompt_key": "sys-x.bin",
    }
    slot = {"id_task": 9}
    d = sc.decide(slot, recorded, "bootA", _led(TURN1), 42, False, "sys-x.bin")
    assert d["action"] == "skip" and d["reason"] == "hot"


def test_cold_on_id_task_change():
    recorded = {
        "boot": "bootA", "id_task": 9, "conversation_id": 42,
        "prompt_key": "sys-x.bin",
    }
    d = sc.decide({"id_task": 11}, recorded, "bootA", _led(TURN1), 42, False, "sys-x.bin")
    assert d["action"] == "restore" and d["bin"] == "conv-42-t1.bin"


def test_cold_on_boot_change_child_restart():
    recorded = {
        "boot": "bootA", "id_task": 9, "conversation_id": 42,
        "prompt_key": "sys-x.bin",
    }
    d = sc.decide({"id_task": 9}, recorded, "bootB", _led(TURN1), 42, False, "sys-x.bin")
    assert d["action"] == "restore"


def test_hot_rejected_when_prompt_head_changed():
    """Issue #557 review: a rebuilt system prompt (workspace move, prompt
    edit) must break BOTH hotness and turn-bin eligibility - the slot
    fingerprint alone would cold-restore a bin baked from the old head."""
    recorded = {
        "boot": "bootA", "id_task": 9, "conversation_id": 42,
        "prompt_key": "sys-old.bin",
    }
    d = sc.decide(
        {"id_task": 9}, recorded, "bootA", _led(TURN1), 42, False, "sys-new.bin"
    )
    assert d["action"] == "skip" and d["reason"] == "no-bin"


def test_stale_prompt_head_falls_back_to_current_base():
    recorded = {
        "boot": "bootA", "id_task": 9, "conversation_id": 42,
        "prompt_key": "sys-old.bin",
    }
    base = {"file": "sys-new.bin", "kind": "base", "n_written": 5}
    d = sc.decide(
        {"id_task": 9}, recorded, "bootA", _led(TURN1, base), 42, False, "sys-new.bin"
    )
    assert d == {"action": "restore", "bin": "sys-new.bin", "reason": "seed-fallback"}


def test_hot_rejected_when_prompt_head_changed():
    """Issue #557 review: a rebuilt system prompt (workspace move, prompt
    edit) must break BOTH hotness and turn-bin eligibility - the slot
    fingerprint alone would cold-restore a bin baked from the old head."""
    recorded = {
        "boot": "bootA", "id_task": 9, "conversation_id": 42,
        "prompt_key": "sys-old.bin",
    }
    d = sc.decide(
        {"id_task": 9}, recorded, "bootA", _led(TURN1), 42, False, "sys-new.bin"
    )
    assert d["action"] == "skip" and d["reason"] == "no-bin"


def test_stale_prompt_head_falls_back_to_current_base():
    recorded = {
        "boot": "bootA", "id_task": 9, "conversation_id": 42,
        "prompt_key": "sys-old.bin",
    }
    base = {"file": "sys-new.bin", "kind": "base", "n_written": 5}
    d = sc.decide(
        {"id_task": 9}, recorded, "bootA", _led(TURN1, base), 42, False, "sys-new.bin"
    )
    assert d == {"action": "restore", "bin": "sys-new.bin", "reason": "seed-fallback"}


def test_new_conversation_never_hot_restores_seed():
    recorded = {"boot": "bootA", "id_task": 9, "conversation_id": 42}
    d = sc.decide(
        {"id_task": 9}, recorded, "bootA",
        _led({"file": "sys-seed.bin", "kind": "base"}), 77, True, "sys-seed.bin"
    )
    assert d["action"] == "restore" and d["bin"] == "sys-seed.bin"
    assert d["reason"] == "seed"


def test_new_conversation_without_seed_skips():
    d = sc.decide({"id_task": 9}, None, "bootA", _led(TURN1), 77, True, "sys-none.bin")
    assert d["action"] == "skip" and d["reason"] == "no-seed-yet"


def test_existing_conversation_without_turn_bins_falls_back_to_seed():
    d = sc.decide(
        {"id_task": 9}, None, "bootA",
        {"bins": [{"file": "sys-s.bin", "kind": "base", "n_written": 1}]},
        42, False, "sys-s.bin",
    )
    assert d["action"] == "restore" and d["reason"] == "seed-fallback"


def test_recorded_task_id_none_is_cold():
    # A fresh child omits id_task entirely (verified live) - never hot.
    recorded = {"boot": "bootA", "id_task": None, "conversation_id": 42}
    d = sc.decide(
        {"id_task": None}, recorded, "bootA", _led(TURN1), 42, False, "sys-x.bin"
    )
    assert d["action"] == "restore"


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------


def test_next_turn_number_from_ledger():
    assert sc.next_turn_number(_led(TURN1), 42) == 2
    assert sc.next_turn_number(_led(), 42) == 1
    t2 = dict(TURN1, file="conv-42-t2.bin", turn=2, ts=2)
    assert sc.next_turn_number(_led(TURN1, t2), 42) == 3


def test_eviction_respects_count_budget():
    bins = [
        dict(TURN1, file=f"conv-42-t{i}.bin", turn=i, ts=i, n_written=1000)
        for i in range(1, 7)
    ]
    ledger = _led(*bins)
    evicted = sc._evict_turn_bins(ledger, 42)
    # MAX_BINS_PER_CONVERSATION = 4 -> oldest two evicted.
    assert evicted == ["conv-42-t1.bin", "conv-42-t2.bin"]
    remaining = [b for b in ledger["bins"] if b["kind"] == "turn"]
    assert len(remaining) == 4


def test_eviction_respects_byte_budget():
    big = dict(TURN1, file="conv-42-t1.bin", turn=1, ts=1, n_written=500 * 1024 * 1024)
    big2 = dict(TURN1, file="conv-42-t2.bin", turn=2, ts=2, n_written=500 * 1024 * 1024)
    ledger = _led(big, big2)
    evicted = sc._evict_turn_bins(ledger, 42)
    # Together over the 800 MB budget -> the oldest goes first.
    assert evicted == ["conv-42-t1.bin"]
    assert [b["file"] for b in ledger["bins"]] == ["conv-42-t2.bin"]


def test_eviction_keeps_newest_bin_even_over_byte_budget():
    """Issue #557 review: a single bin over the 800 MB budget (realistic at
    ~200 MB/bin live) must stay in the ledger - evicting it would leave a
    cold conversation with no restore point and restart its numbering."""
    huge = dict(
        TURN1, file="conv-42-t1.bin", turn=1, ts=1, n_written=900 * 1024 * 1024
    )
    ledger = _led(huge)
    assert sc._evict_turn_bins(ledger, 42) == []
    assert [b["file"] for b in ledger["bins"]] == ["conv-42-t1.bin"]


def test_eviction_keeps_newest_bin_even_over_byte_budget():
    """Issue #557 review: a single bin over the 800 MB budget (realistic at
    ~200 MB/bin live) must stay in the ledger - evicting it would leave a
    cold conversation with no restore point and restart its numbering."""
    huge = dict(
        TURN1, file="conv-42-t1.bin", turn=1, ts=1, n_written=900 * 1024 * 1024
    )
    ledger = _led(huge)
    assert sc._evict_turn_bins(ledger, 42) == []
    assert [b["file"] for b in ledger["bins"]] == ["conv-42-t1.bin"]


def test_eviction_never_touches_other_conversations():
    other = dict(TURN1, file="conv-43-t1.bin", conversation_id=43, turn=1, ts=9)
    ledger = _led(other)
    assert sc._evict_turn_bins(ledger, 42) == []
    assert len(ledger["bins"]) == 1


def test_upsert_replaces_same_name():
    ledger = sc._upsert_bin(_led(), dict(TURN1))
    ledger = sc._upsert_bin(ledger, dict(TURN1, n_tokens=999))
    assert len(ledger["bins"]) == 1 and ledger["bins"][0]["n_tokens"] == 999


# ---------------------------------------------------------------------------
# Naming / hashing / usage parsing
# ---------------------------------------------------------------------------


def test_base_bin_name_stable_and_keyed():
    a = sc.base_bin_name("http://h:8080/v1", "x2.m", "SYS")
    b = sc.base_bin_name("http://h:8080/v1", "x2.m", "SYS")
    c = sc.base_bin_name("http://h:8080/v1", "x2.m", "OTHER")
    assert a == b and a != c and a.startswith("sys-") and a.endswith(".bin")


def test_cached_tokens_from_usage_blob():
    assert sc.cached_tokens({"prompt_tokens_details": {"cached_tokens": 8}}) == 8
    assert sc.cached_tokens({"prompt_tokens_details": {"cached_tokens": 0}}) == 0
    assert sc.cached_tokens(None) is None
    assert sc.cached_tokens({}) is None


# ---------------------------------------------------------------------------
# resolve_preset: the hard provider gate
# ---------------------------------------------------------------------------


def test_resolve_preset_port_gate():
    assert sc.resolve_preset({"api_base": "http://192.168.1.170:8080/v1", "model": "x2.m"}) == "x2.m"
    assert sc.resolve_preset({"api_base": "http://localhost:8080/v1", "model": "m"}) == "m"
    assert sc.resolve_preset({"api_base": "https://api.openai.com/v1", "model": "gpt"}) is None
    assert sc.resolve_preset({"api_base": "https://openrouter.ai/api/v1", "model": "m"}) is None
    assert sc.resolve_preset({"api_base": "https://api.z.ai/api/coding/paas/v4", "model": "glm"}) is None
    assert sc.resolve_preset({"api_base": "http://localhost:11434/v1", "model": "llama3.2"}) is None
    assert sc.resolve_preset({"api_base": "http://localhost:8080/v1", "model": ""}) is None


# ---------------------------------------------------------------------------
# Async paths with faked HTTP: degrade-silent + gate/flush bookkeeping
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _models_payload(preset="x2.m", args=None):
    return {
        "data": [
            {
                "id": preset,
                "status": {"value": "loaded", "args": args or ["--port", "1"]},
            }
        ]
    }


def _slot_payload(id_task=5):
    return [{"id": 0, "is_processing": False, "id_task": id_task}]


class _Router:
    """Scriptable stand-in for the router: counts calls, serves canned
    payloads, bumps id_task on every task (saves/restores included)."""

    def __init__(self):
        self.models = _models_payload()
        self.slot = _slot_payload()
        self.gets: list[str] = []
        self.posts: list[tuple[str, str]] = []
        self.task_bump = True

    async def get(self, url, **kw):
        self.gets.append(url)
        if "/v1/models" in url:
            return _FakeResp(200, self.models)
        if "/slots?" in url:
            return _FakeResp(200, self.slot)
        return _FakeResp(404)

    async def post(self, url, json=None, **kw):
        self.posts.append((url, (json or {}).get("filename", "")))
        if self.task_bump:
            self.slot = _slot_payload((self.slot[0].get("id_task") or 0) + 1)
        return _FakeResp(200, {"id_slot": 0, "n_saved": 120, "n_written": 4096})

    def install(self, monkeypatch):
        # _get_json's contract: status-check + unwrap to the payload.
        async def _get(base, path, timeout):
            resp = await self.get(f"{base}{path}")
            if resp.status_code != 200:
                raise sc._Degraded(f"HTTP {resp.status_code} for {path}")
            return resp.json()

        async def _post(base, id_slot, action, model, filename=""):
            # _post_action's contract: status-check + unwrap to dict.
            resp = await self.post(
                f"{base}/slots/{id_slot}?action={action}", json={"model": model, "filename": filename}
            )
            if resp.status_code != 200:
                raise sc._Degraded(f"HTTP {resp.status_code} for {action}")
            return resp.json()

        monkeypatch.setattr(sc, "_get_json", _get)
        monkeypatch.setattr(sc, "_post_action", _post)


@pytest.fixture
def clean_env(monkeypatch):
    sc.reset_state()
    yield
    sc.reset_state()

@pytest.fixture
def router_cfg(monkeypatch):
    """Point load_config (both import sites) at an :8080 provider."""
    cfg = {
        "api_base": "http://h:8080/v1",
        "api_key": "",
        "model": "x2.m",
        "reasoning_effort": "",
        "active_provider": "llamacpp",
        "providers": {
            "llamacpp": {"api_base": "http://h:8080/v1", "api_key": "", "model": "x2.m"}
        },
    }
    monkeypatch.setattr("backend.agent.config.load_config", lambda: cfg)
    monkeypatch.setattr("backend.agent.model_client.load_config", lambda: cfg)


@pytest.mark.asyncio
async def test_gate_restores_cold_then_flush_saves(monkeypatch, tmp_path, clean_env, router_cfg):
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")

    await sc.gate(
        model="", effort="", conversation_id=42,
        is_new_conversation=True, system_prompt="S" * 2000,
    )
    # New conversation + no seed yet -> skip, but the slot went "hot" for
    # the conversation, so the flush below is allowed to save.
    assert all("restore" not in u for u, _ in router.posts)

    await sc.flush(
        model="", effort="", conversation_id=42,
        system_prompt="S" * 2000,
        usage={"prompt_tokens_details": {"cached_tokens": 40}},
        is_first_turn=True,
    )
    saves = [f for _, f in router.posts]
    # Turn bin + the persona's seed bin (first save for this key).
    assert saves.count("conv-42-t1.bin") == 1
    assert any(f.startswith("sys-") for f in saves)
    ledger = json.loads((tmp_path / "led.json").read_text("utf-8"))
    files = [b["file"] for b in ledger["bins"]]
    assert "conv-42-t1.bin" in files and any(f.startswith("sys-") for f in files)
    # Atomic ledger write (issue #557 review): no torn-write temp left over.
    assert not (tmp_path / "led.json.tmp").exists()
    # Atomic ledger write (issue #557 review): no torn-write temp left over.
    assert not (tmp_path / "led.json.tmp").exists()

    # Second conversation, same persona: gate restores the seed bin.
    await sc.gate(
        model="", effort="", conversation_id=43,
        is_new_conversation=True, system_prompt="S" * 2000,
    )
    assert any(
        "action=restore" in u and f == next(
            b["file"] for b in ledger["bins"] if b["kind"] == "base"
        )
        for u, f in router.posts
    )


@pytest.mark.asyncio
async def test_turn_end_flush_saves_and_stays_hot(monkeypatch, tmp_path, clean_env, router_cfg):
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")

    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=True, system_prompt="S" * 2000)
    await sc.flush(model="", effort="", conversation_id=42,
                   system_prompt="S" * 2000,
                   usage={"prompt_tokens_details": {"cached_tokens": 5}},
                   is_first_turn=True)
    posts_after_t1 = len(router.posts)

    # Same conversation continues: gate must SKIP (recorded fingerprint
    # matches) and not restore over the fresher hot tail.
    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=False, system_prompt="S" * 2000)
    assert not any("restore" in u for u, _ in router.posts)
    # The turn-2 flush then saves conv-42-t2.bin.
    await sc.flush(model="", effort="", conversation_id=42,
                   system_prompt="S" * 2000,
                   usage={"prompt_tokens_details": {"cached_tokens": 5}})
    saves = [f for _, f in router.posts]
    assert saves.count("conv-42-t2.bin") == 1
    # Seed bin was NOT re-saved (key already has a bin).
    assert sum(1 for f in saves if f.startswith("sys-")) == 1


@pytest.mark.asyncio
async def test_flush_without_gate_is_noop(monkeypatch, tmp_path, clean_env, router_cfg):
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    await sc.flush(model="", effort="", conversation_id=42,
                   system_prompt="S" * 2000, usage=None)
    assert router.posts == []


@pytest.mark.asyncio
async def test_flush_non_first_turn_never_seeds(monkeypatch, tmp_path, clean_env, router_cfg):
    """Issue #557 review: only a conversation's first turn may create the
    persona seed - a pre-cache conversation's first flush (its turn
    numbering also starts at 1) must not bake its long prefix in."""
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")

    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=True, system_prompt="S" * 2000)
    await sc.flush(model="", effort="", conversation_id=42,
                   system_prompt="S" * 2000, usage=None, is_first_turn=False)
    assert not any(f.startswith("sys-") for _, f in router.posts)


@pytest.mark.asyncio
async def test_flush_non_first_turn_never_seeds(monkeypatch, tmp_path, clean_env, router_cfg):
    """Issue #557 review: only a conversation's first turn may create the
    persona seed - a pre-cache conversation's first flush (its turn
    numbering also starts at 1) must not bake its long prefix in."""
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")

    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=True, system_prompt="S" * 2000)
    await sc.flush(model="", effort="", conversation_id=42,
                   system_prompt="S" * 2000, usage=None, is_first_turn=False)
    assert not any(f.startswith("sys-") for _, f in router.posts)


@pytest.mark.asyncio
async def test_degrade_paths_never_raise(monkeypatch, tmp_path, clean_env, router_cfg):
    async def boom(*a, **k):
        raise RuntimeError("router on fire")

    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    monkeypatch.setattr(sc, "_probe", boom)
    await sc.gate(model="", effort="", conversation_id=1,
                  is_new_conversation=True, system_prompt="S" * 2000)
    await sc.flush(model="", effort="", conversation_id=1,
                   system_prompt="S" * 2000, usage=None)


@pytest.mark.asyncio
async def test_gate_busy_lock_proceeds_ungated(monkeypatch, tmp_path, clean_env, router_cfg):
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    lock = sc.slot_lock("http://h:8080/v1", "x2.m")
    await lock.acquire()
    try:
        await sc.gate(model="", effort="", conversation_id=42,
                      is_new_conversation=False, system_prompt="S" * 2000)
        # Busy -> no probes, no posts, and no hotness record.
        assert router.gets == [] and router.posts == []
        assert sc._SLOT_STATE == {}
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_unloaded_model_degrades_before_slots_probe(monkeypatch, clean_env, router_cfg):
    router = _Router()
    router.models = {"data": [{"id": "x2.m", "status": {"value": "unloaded"}}]}
    router.install(monkeypatch)
    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=True, system_prompt="S" * 2000)
    # The hang-avoidance ordering: /v1/models consulted, /slots never hit.
    assert any("/v1/models" in u for u in router.gets)
    assert not any("/slots?" in u for u in router.gets)


@pytest.mark.asyncio
async def test_resolve_call_cfg_routing_is_respected(monkeypatch, tmp_path, clean_env):
    """provider::model overrides must retarget the gate (issue #557 item 9)."""
    import backend.agent.model_client as mc

    cfg = {
        "api_base": "https://api.openai.com/v1",
        "api_key": "k",
        "model": "gpt",
        "reasoning_effort": "",
        "active_provider": "openai",
        "providers": {
            "llamacpp": {"api_base": "http://h:8080/v1", "api_key": "", "model": "x2.m"}
        },
    }
    monkeypatch.setattr(mc, "load_config", lambda: cfg)
    monkeypatch.setattr("backend.agent.config.load_config", lambda: cfg)
    resolved = mc._resolve_call_cfg(dict(cfg), "llamacpp::x2.m", "")
    assert resolved["api_base"] == "http://h:8080/v1"
    assert sc.resolve_preset(resolved) == "x2.m"
