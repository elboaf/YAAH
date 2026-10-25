"""Issue #557: slot-cache spec rework - two cache points.

1. BASE bin bootstrapped at the gate (aux [system] prefill + save) on the
   first message that needs it - independent of run completion.
2. ONE per-conversation bin (conv-<id>.bin), overwritten at each turn end.

No network: every server touch is faked. The live-API behavior itself was
verified against the real router during fact-checking (see the issue);
the "wb"-truncate save/overwrite semantics were verified against
llama.cpp source (server-context.cpp + llama-context.cpp).
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


CONV = {
    "file": "conv-42.bin",
    "kind": "turn",
    "conversation_id": 42,
    "n_tokens": 100,
    "n_written": 1000,
    "boot": "bootA",
    "id_task_at_save": 7,
    "prompt_key": "sys-x.bin",
    "ts": 1,
}

BASE = {
    "file": "sys-x.bin",
    "kind": "base",
    "n_tokens": 90,
    "n_written": 900,
    "boot": "bootA",
    "id_task_at_save": 6,
    "prompt_key": "sys-x.bin",
    "ts": 1,
}

HOT_RECORD = {
    "boot": "bootA",
    "id_task": 9,
    "conversation_id": 42,
    "prompt_key": "sys-x.bin",
}


def test_gate_skips_when_no_slot_info():
    d = sc.decide(None, None, "bootA", _led(), 42, False, "sys-x.bin")
    assert d == {"action": "skip", "bin": None, "reason": "no-slot-info"}


def test_hot_when_boot_id_task_conversation_and_prompt_match():
    d = sc.decide(
        {"id_task": 9}, dict(HOT_RECORD), "bootA", _led(CONV), 42, False, "sys-x.bin"
    )
    assert d["action"] == "skip" and d["reason"] == "hot"


def test_cold_on_id_task_change_restores_own_conv_bin():
    d = sc.decide(
        {"id_task": 11}, dict(HOT_RECORD), "bootA", _led(CONV), 42, False, "sys-x.bin"
    )
    assert d == {"action": "restore", "bin": "conv-42.bin", "reason": "conv-bin"}


def test_cold_on_boot_change_child_restart():
    d = sc.decide(
        {"id_task": 9}, dict(HOT_RECORD), "bootB", _led(CONV), 42, False, "sys-x.bin"
    )
    assert d == {"action": "restore", "bin": "conv-42.bin", "reason": "conv-bin"}


def test_recorded_task_id_none_is_cold():
    # A fresh child omits id_task entirely (verified live) - never hot.
    recorded = dict(HOT_RECORD, id_task=None)
    d = sc.decide(
        {"id_task": None}, recorded, "bootA", _led(CONV), 42, False, "sys-x.bin"
    )
    assert d["action"] == "restore"


def test_hot_rejected_when_prompt_head_changed():
    """Issue #557 review: a rebuilt system prompt (workspace move, prompt
    edit) must break hotness AND conv-bin eligibility."""
    d = sc.decide(
        {"id_task": 9},
        dict(HOT_RECORD, prompt_key="sys-old.bin"),
        "bootA",
        _led(CONV),
        42,
        False,
        "sys-new.bin",
    )
    assert d == {"action": "bootstrap", "bin": "sys-new.bin", "reason": "need-base"}


def test_stale_prompt_head_falls_back_to_current_base():
    d = sc.decide(
        {"id_task": 9},
        dict(HOT_RECORD, prompt_key="sys-old.bin"),
        "bootA",
        _led(CONV, dict(BASE, file="sys-new.bin", prompt_key="sys-new.bin")),
        42,
        False,
        "sys-new.bin",
    )
    assert d == {"action": "restore", "bin": "sys-new.bin", "reason": "base-fallback"}


def test_conv_bin_of_other_prompt_head_is_invisible():
    """A conv bin baked under an old prompt head must not be restored."""
    d = sc.decide(
        {"id_task": 9},
        dict(HOT_RECORD, prompt_key="sys-old.bin"),
        "bootA",
        _led(dict(CONV, prompt_key="sys-old.bin")),
        42,
        False,
        "sys-new.bin",
    )
    assert d["action"] == "bootstrap"


def test_new_conversation_never_hot_restores_base():
    d = sc.decide(
        {"id_task": 9}, dict(HOT_RECORD), "bootA", _led(BASE), 77, True, "sys-x.bin"
    )
    assert d == {"action": "restore", "bin": "sys-x.bin", "reason": "seed"}


def test_new_conversation_without_base_bootstraps():
    d = sc.decide({"id_task": 9}, None, "bootA", _led(CONV), 77, True, "sys-none.bin")
    assert d == {"action": "bootstrap", "bin": "sys-none.bin", "reason": "need-base"}


def test_existing_conversation_without_conv_bin_falls_back_to_base():
    d = sc.decide(
        {"id_task": 9}, None, "bootA", _led(BASE), 42, False, "sys-x.bin"
    )
    assert d == {"action": "restore", "bin": "sys-x.bin", "reason": "base-fallback"}


def test_existing_conversation_no_checkpoints_bootstraps():
    # Also warms the slot for the turn - strictly better than skipping.
    d = sc.decide({"id_task": 9}, None, "bootA", _led(), 42, False, "sys-x.bin")
    assert d == {"action": "bootstrap", "bin": "sys-x.bin", "reason": "need-base"}


# ---------------------------------------------------------------------------
# Naming / hashing / usage parsing
# ---------------------------------------------------------------------------


def test_base_bin_name_stable_and_keyed():
    a = sc.base_bin_name("http://h:8080/v1", "x2.m", "SYS")
    b = sc.base_bin_name("http://h:8080/v1", "x2.m", "SYS")
    c = sc.base_bin_name("http://h:8080/v1", "x2.m", "OTHER")
    assert a == b and a != c and a.startswith("sys-") and a.endswith(".bin")


def test_conv_bin_name_is_one_stable_file_per_conversation():
    assert sc.conv_bin_name(42) == "conv-42.bin"
    assert sc.conv_bin_name("42") == "conv-42.bin"


def test_upsert_replaces_same_name():
    ledger = sc._upsert_bin(_led(), dict(CONV))
    ledger = sc._upsert_bin(ledger, dict(CONV, n_tokens=999))
    assert len(ledger["bins"]) == 1 and ledger["bins"][0]["n_tokens"] == 999


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
                f"{base}/slots/{id_slot}?action={action}",
                json={"model": model, "filename": filename},
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


def _fake_chat(monkeypatch, calls: list, payload=None, raise_exc=None):
    async def chat(messages, tools=None, stream=False, model="", effort="",
                   max_tokens=None):
        calls.append(
            {"messages": messages, "tools": tools, "stream": stream,
             "model": model, "effort": effort, "max_tokens": max_tokens}
        )
        if raise_exc is not None:
            raise raise_exc
        return payload or {"choices": [{"message": {"content": ""}}]}

    monkeypatch.setattr("backend.agent.model_client.chat", chat)


def _seed_base_ledger(monkeypatch, tmp_path, system_prompt):
    """Seed the ledger with the persona BASE under its REAL computed name
    (base_bin_name hashes api_base+model+system_prompt)."""
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    name = sc.base_bin_name("http://h:8080/v1", "x2.m", system_prompt)
    sc._write_ledger(
        "http://h:8080/v1",
        "x2.m",
        {"bins": [dict(BASE, file=name, prompt_key=name)]},
    )


@pytest.mark.asyncio
async def test_first_message_bootstraps_base(monkeypatch, tmp_path, clean_env, router_cfg):
    """THE spec point: the BASE exists after the first message's gate -
    aux [system] prefill + save - before any of the turn's own activity."""
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    chats: list = []
    _fake_chat(monkeypatch, chats)

    await sc.gate(
        model="", effort="", conversation_id=42,
        is_new_conversation=True, system_prompt="S" * 2000,
    )
    # Exactly one aux prefill, carrying EXACTLY the system prompt.
    assert len(chats) == 1
    # System + a one-char user turn: strict templates 500 on system-only.
    assert chats[0]["messages"] == [
        {"role": "system", "content": "S" * 2000},
        {"role": "user", "content": "."},
    ]
    assert not chats[0].get("tools") and chats[0]["stream"] is False
    # Round-3: generation is capped so a reasoning model can't think
    # under the slot lock.
    assert chats[0]["max_tokens"] == 1
    # The persona BASE was saved, and nothing was restored.
    saves = [f for _, f in router.posts]
    assert len(saves) == 1 and saves[0].startswith("sys-")
    assert not any("restore" in u for u, _ in router.posts)
    # The ledger knows the BASE; the fingerprint is recorded post-save.
    ledger = json.loads((tmp_path / "led.json").read_text("utf-8"))
    assert [b["kind"] for b in ledger["bins"]] == ["base"]
    # No torn-write temp left behind.
    assert not (tmp_path / "led.json.tmp").exists()


@pytest.mark.asyncio
async def test_probe_hits_router_surface_not_api_base(monkeypatch, clean_env):
    """Round-3 regression: _probe must hit the ROUTER surface. An api_base
    of http://host:8080/v1 must not be prepended again - the doubled
    /v1/v1/ prefix 404s and silently disables the whole module."""
    router = _Router()
    router.install(monkeypatch)
    probed = await sc._probe("http://h:8080/v1", "x2.m")
    assert probed is not None
    assert router.gets == [
        "http://h:8080/v1/models",
        "http://h:8080/slots?model=x2.m",
    ]


def test_build_payload_max_tokens_opt_in():
    """Round-3: real chats carry NO max_tokens (payload byte-identical to
    before); only an explicit opt-in (aux prefill) caps generation."""
    from backend.agent.model_client import _build_payload

    cfg = {"model": "m"}
    assert "max_tokens" not in _build_payload(cfg, None, False)
    assert _build_payload(cfg, None, False, max_tokens=1)["max_tokens"] == 1


@pytest.mark.asyncio
async def test_bootstrap_is_cancellable(monkeypatch, tmp_path, clean_env, router_cfg):
    """Round-3: Stop during the BASE bootstrap aborts it - no save, no
    ledger write, no fingerprint recorded."""
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")

    async def chat(messages, tools=None, stream=False, model="", effort="",
                   max_tokens=None):
        await asyncio.Event().wait()  # never completes

    monkeypatch.setattr("backend.agent.model_client.chat", chat)
    cancel_ev = asyncio.Event()
    cancel_ev.set()
    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=True, system_prompt="S" * 2000,
                  cancel_ev=cancel_ev)
    assert router.posts == []
    assert sc._SLOT_STATE == {}
    # Cancelled before the save: the ledger is never even created.
    assert not (tmp_path / "led.json").exists()


@pytest.mark.asyncio
async def test_bootstrap_prefill_failure_is_contained(monkeypatch, tmp_path, clean_env, router_cfg):
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    chats: list = []
    _fake_chat(monkeypatch, chats, raise_exc=RuntimeError("prefill on fire"))

    await sc.gate(
        model="", effort="", conversation_id=42,
        is_new_conversation=True, system_prompt="S" * 2000,
    )
    assert router.posts == []  # nothing was saved
    # But the observation IS recorded: the turn-end flush must still run
    # (round-4: a failed bootstrap used to disable the conv save too).
    assert sc._SLOT_STATE


@pytest.mark.asyncio
async def test_bootstrap_failure_still_allows_turn_end_flush(monkeypatch, tmp_path, clean_env, router_cfg):
    """Live diagnosis 2026-10-25: a failed BASE bootstrap used to leave no
    observation record, which silently disabled the turn-end conv save.
    The flush must still checkpoint the conversation."""
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    _fake_chat(monkeypatch, [], raise_exc=RuntimeError("template on fire"))

    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=True, system_prompt="S" * 2000)
    # The probe succeeded, so the conversation is recorded for the flush
    # even though the bootstrap degraded.
    assert sc._SLOT_STATE

    await sc.flush(model="", effort="", conversation_id=42,
                   system_prompt="S" * 2000, usage=None)
    assert any(f.startswith("conv-") for _, f in router.posts)
    ledger = json.loads((tmp_path / "led.json").read_text("utf-8"))
    assert any(b["kind"] == "turn" for b in ledger["bins"])


@pytest.mark.asyncio
async def test_bootstrap_failure_still_allows_turn_end_flush(monkeypatch, tmp_path, clean_env, router_cfg):
    """Live diagnosis 2026-10-25: a failed BASE bootstrap used to leave no
    observation record, which silently disabled the turn-end conv save.
    The flush must still checkpoint the conversation."""
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    _fake_chat(monkeypatch, [], raise_exc=RuntimeError("template on fire"))

    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=True, system_prompt="S" * 2000)
    # The probe succeeded, so the conversation is recorded for the flush
    # even though the bootstrap degraded.
    assert sc._SLOT_STATE

    await sc.flush(model="", effort="", conversation_id=42,
                   system_prompt="S" * 2000, usage=None)
    assert any(f.startswith("conv-") for _, f in router.posts)
    ledger = json.loads((tmp_path / "led.json").read_text("utf-8"))
    assert any(b["kind"] == "turn" for b in ledger["bins"])


@pytest.mark.asyncio
async def test_bootstrap_failure_still_allows_turn_end_flush(monkeypatch, tmp_path, clean_env, router_cfg):
    """Live diagnosis 2026-10-25: a failed BASE bootstrap used to leave no
    observation record, which silently disabled the turn-end conv save.
    The flush must still checkpoint the conversation."""
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    _fake_chat(monkeypatch, [], raise_exc=RuntimeError("template on fire"))

    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=True, system_prompt="S" * 2000)
    # The probe succeeded, so the conversation is recorded for the flush
    # even though the bootstrap degraded.
    assert sc._SLOT_STATE

    await sc.flush(model="", effort="", conversation_id=42,
                   system_prompt="S" * 2000, usage=None)
    assert any(f.startswith("conv-") for _, f in router.posts)
    ledger = json.loads((tmp_path / "led.json").read_text("utf-8"))
    assert any(b["kind"] == "turn" for b in ledger["bins"])


@pytest.mark.asyncio
async def test_bootstrap_skips_short_system_prompt(monkeypatch, tmp_path, clean_env, router_cfg):
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    chats: list = []
    _fake_chat(monkeypatch, chats)

    await sc.gate(
        model="", effort="", conversation_id=42,
        is_new_conversation=True, system_prompt="S" * 100,
    )
    assert chats == [] and router.posts == []
    # But the gate still recorded the observation so the turn-end flush
    # is allowed to checkpoint the conversation.
    assert sc._SLOT_STATE


@pytest.mark.asyncio
async def test_new_conversation_restores_base_without_prefill(monkeypatch, tmp_path, clean_env, router_cfg):
    router = _Router()
    router.install(monkeypatch)
    _seed_base_ledger(monkeypatch, tmp_path, "S" * 2000)
    chats: list = []
    _fake_chat(monkeypatch, chats)

    await sc.gate(
        model="", effort="", conversation_id=43,
        is_new_conversation=True, system_prompt="S" * 2000,
    )
    assert chats == []  # BASE exists: no aux prefill
    base_name = sc.base_bin_name("http://h:8080/v1", "x2.m", "S" * 2000)
    assert any("action=restore" in u and f == base_name for u, f in router.posts)


@pytest.mark.asyncio
async def test_turn_end_flush_saves_and_stays_hot(monkeypatch, tmp_path, clean_env, router_cfg):
    router = _Router()
    router.install(monkeypatch)
    _seed_base_ledger(monkeypatch, tmp_path, "S" * 2000)
    chats: list = []
    _fake_chat(monkeypatch, chats)

    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=True, system_prompt="S" * 2000)
    # The persona BASE already existed: the gate restored it, no prefill.
    assert chats == []
    await sc.flush(model="", effort="", conversation_id=42,
                   system_prompt="S" * 2000,
                   usage={"prompt_tokens_details": {"cached_tokens": 5}})

    # Same conversation continues: gate must SKIP (recorded fingerprint
    # matches) and not restore over the fresher hot tail.
    posts_after_t1 = len(router.posts)
    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=False, system_prompt="S" * 2000)
    assert len(router.posts) == posts_after_t1  # hot: nothing posted
    # The turn-2 flush then re-saves THE SAME bin name.
    await sc.flush(model="", effort="", conversation_id=42,
                   system_prompt="S" * 2000,
                   usage={"prompt_tokens_details": {"cached_tokens": 5}})
    saves = [f for _, f in router.posts if f.startswith("conv-")]
    assert saves == ["conv-42.bin", "conv-42.bin"]
    ledger = json.loads((tmp_path / "led.json").read_text("utf-8"))
    convs = [b for b in ledger["bins"] if b["kind"] == "turn"]
    assert len(convs) == 1  # overwritten in the ledger, not accumulated


@pytest.mark.asyncio
async def test_flush_without_gate_is_noop(monkeypatch, tmp_path, clean_env, router_cfg):
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    await sc.flush(model="", effort="", conversation_id=42,
                   system_prompt="S" * 2000, usage=None)
    assert router.posts == []


@pytest.mark.asyncio
async def test_degrade_paths_never_raise(monkeypatch, tmp_path, clean_env, router_cfg):
    async def boom(*a, **k):
        raise RuntimeError("router on fire")

    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    monkeypatch.setattr(sc, "_probe", boom)
    _fake_chat(monkeypatch, [], raise_exc=RuntimeError("prefill on fire"))
    await sc.gate(model="", effort="", conversation_id=1,
                  is_new_conversation=True, system_prompt="S" * 2000)
    await sc.flush(model="", effort="", conversation_id=1,
                   system_prompt="S" * 2000, usage=None)


@pytest.mark.asyncio
async def test_gate_busy_lock_proceeds_ungated(monkeypatch, tmp_path, clean_env, router_cfg):
    router = _Router()
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    chats: list = []
    _fake_chat(monkeypatch, chats)
    lock = sc.slot_lock("http://h:8080/v1", "x2.m")
    await lock.acquire()
    try:
        await sc.gate(model="", effort="", conversation_id=42,
                      is_new_conversation=False, system_prompt="S" * 2000)
        # Busy -> no probes, no posts, no prefill, and no hotness record.
        assert router.gets == [] and router.posts == [] and chats == []
        assert sc._SLOT_STATE == {}
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_unloaded_model_degrades_before_slots_probe(monkeypatch, clean_env, router_cfg):
    router = _Router()
    router.models = {"data": [{"id": "x2.m", "status": {"value": "unloaded"}}]}
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    chats: list = []
    _fake_chat(monkeypatch, chats)
    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=True, system_prompt="S" * 2000)
    # The hang-avoidance ordering: /v1/models consulted, /slots never hit.
    # ROUND5: with an EMPTY ledger there is no bin worth a load cycle, so
    # the gate still degrades silently - no aux prefill against an
    # unloaded child.
    assert any("/v1/models" in u for u in router.gets)
    assert not any("/slots?" in u for u in router.gets)
    assert chats == []


@pytest.mark.asyncio
async def test_unloaded_preset_with_seed_warms_and_restores(
    monkeypatch, tmp_path, clean_env, router_cfg,
):
    """ROUND5 live-diagnosed 2026-10-25: after a router restart the gate
    used to degrade silently and the turn paid load + FULL prefill with
    bins sitting unused. Now: seed exists + preset unloaded -> tiny aux
    warm prefill (spawns the child), re-probe, restore the seed."""
    name = sc.base_bin_name("http://h:8080/v1", "x2.m", "S" * 2000)
    router = _Router()
    router.models = {"data": [{"id": "x2.m", "status": {"value": "unloaded"}}]}
    router.slot = _slot_payload(id_task=0)

    async def chat(messages, tools=None, stream=False, model="", effort="",
                   max_tokens=None):
        chats.append({"messages": messages, "max_tokens": max_tokens})
        # The load completed: the router now reports the child loaded.
        router.models = _models_payload()
        router.slot = _slot_payload(id_task=1)
        return {"choices": [{"message": {"content": ""}}]}

    chats: list = []
    monkeypatch.setattr("backend.agent.model_client.chat", chat)
    router.install(monkeypatch)
    _seed_base_ledger(monkeypatch, tmp_path, "S" * 2000)

    await sc.gate(model="", effort="", conversation_id=77,
                  is_new_conversation=True, system_prompt="S" * 2000)

    # Warm prefill: bare user turn, bounded to one token.
    assert len(chats) == 1
    assert chats[0]["messages"] == [{"role": "user", "content": "."}]
    assert chats[0]["max_tokens"] == 1
    # Then the normal restore path ran against the LOADED child.
    assert ("http://h:8080/slots/0?action=restore", name) in router.posts


@pytest.mark.asyncio
async def test_unloaded_preset_without_matching_bins_stays_silent(
    monkeypatch, tmp_path, clean_env, router_cfg,
):
    """Unloaded + no bins this turn would restore -> degrade as before
    (no load cycle for a persona that has nothing banked yet)."""
    router = _Router()
    router.models = {"data": [{"id": "x2.m", "status": {"value": "unloaded"}}]}
    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    sc._write_ledger("http://h:8080/v1", "x2.m", {"bins": []})
    chats: list = []
    _fake_chat(monkeypatch, chats)
    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=True, system_prompt="S" * 2000)
    assert chats == []
    assert not any("action=restore" in u for u, _ in router.posts)


@pytest.mark.asyncio
async def test_unloaded_preset_router_error_stays_silent(
    monkeypatch, tmp_path, clean_env, router_cfg,
):
    """A router that ERRORS on /v1/models is not 'unloaded' - degrade
    exactly as before, never warm on a doubt."""
    router = _Router()
    router.models = {"data": [{"id": "x2.m", "status": {"value": "unloaded"}}]}

    async def broken_get(url, **kw):
        raise sc._Degraded("router down")

    async def _get(base, path, timeout):
        await broken_get(url)
        return {}  # pragma: no cover

    router.install(monkeypatch)
    monkeypatch.setattr(sc, "_get_json", _get)
    monkeypatch.setattr(sc, "_ledger_path", lambda a, m: tmp_path / "led.json")
    chats: list = []
    _fake_chat(monkeypatch, chats)
    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=True, system_prompt="S" * 2000)
    assert chats == []


@pytest.mark.asyncio
async def test_flush_survives_one_probe_blip(
    monkeypatch, tmp_path, clean_env, router_cfg,
):
    """ROUND5: a transient probe failure at turn end must not erase the
    save this turn earned (one bounded retry, then the save lands)."""
    router = _Router()
    router.install(monkeypatch)
    _seed_base_ledger(monkeypatch, tmp_path, "S" * 2000)
    await sc.gate(model="", effort="", conversation_id=42,
                  is_new_conversation=True, system_prompt="S" * 2000)
    assert any("action=restore" in u for u, _ in router.posts)

    real_probe = sc._probe
    state = {"calls": 0}

    async def flaky(api_base, preset):
        state["calls"] += 1
        if state["calls"] == 1:
            return None  # one blip
        return await real_probe(api_base, preset)

    monkeypatch.setattr(sc, "_probe", flaky)
    await sc.flush(model="", effort="", conversation_id=42,
                   system_prompt="S" * 2000)
    assert state["calls"] >= 2
    conv = sc.conv_bin_name(42)
    assert any(fn == conv for u, fn in router.posts if "action=save" in u)


def test_warm_candidate_matrix():
    base = dict(BASE, file="sys-x.bin", prompt_key="sys-x.bin")
    conv = dict(CONV, prompt_key="sys-x.bin")
    # New conversation: the seed is the warm target.
    assert sc._warm_candidate(_led(base), 42, True, "sys-x.bin") == "sys-x.bin"
    assert sc._warm_candidate(_led(base), 42, True, "sys-other.bin") is None
    # Existing conversation: its own conv bin wins.
    assert sc._warm_candidate(_led(base, conv), 42, False, "sys-x.bin") == "conv-42.bin"
    # Existing conversation, no conv bin: seed-fallback.
    assert sc._warm_candidate(_led(base), 42, False, "sys-x.bin") == "sys-x.bin"
    # Nothing banked: not worth a load cycle.
    assert sc._warm_candidate(_led(), 42, False, "sys-x.bin") is None


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
        "providers": {"openai": {"api_base": "https://api.openai.com/v1", "api_key": "k", "model": "gpt"}},
    }
    monkeypatch.setattr("backend.agent.config.load_config", lambda: cfg)
    resolved = mc._resolve_call_cfg(dict(cfg), "openai::gpt", "")
    assert resolved["model"] == "gpt"
