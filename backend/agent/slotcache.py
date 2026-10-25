"""Issue #557: disk-backed prompt cache via llama.cpp slot save/restore.

Two cache points per (router, model, system prompt) "persona":

1. BASE bin - the bare system prompt, bootstrapped at the FIRST MESSAGE
   that needs it: at the turn-start gate (before any of the turn's model
   activity) a tiny auxiliary prefill of exactly the system prompt is
   sent, then the slot is saved under the persona's BASE name. Run
   completion is irrelevant - a crash or Stop right after the first
   message still leaves the BASE banked. From then on every NEW
   conversation on that persona restores the BASE at the gate instead of
   re-prefilling the system prompt.
2. Per-conversation bin - ONE stable file per conversation
   (conv-<id>.bin), OVERWRITTEN by the turn-end flush. Verified against
   llama.cpp source: the state save opens "wb" (truncate) and appends
   only its own checkpoint/draft appendix, so re-saving a name replaces
   the checkpoint cleanly. A conversation that went cold (child swap,
   restart, sibling eviction) restores its own last checkpoint.

The gate runs BEFORE the turn's first model call - never mid-run - and
the flush only after the turn's model activity is done. Both are
best-effort by contract: any doubt degrades to "no cache", never raises.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# Personas with a shorter system prompt are not worth a disk round-trip:
# the BASE is only bootstrapped when the head is at least this big.
BASE_MIN_SYSTEM_CHARS = 1024


# ---------------------------------------------------------------------------
# Volatile per-process slot state
# ---------------------------------------------------------------------------

_SLOT_STATE: dict[str, dict] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def _state_key(api_base: str, model: str) -> str:
    return f"{api_base}::{model}"


def slot_lock(api_base: str, model: str) -> asyncio.Lock:
    return _LOCKS.setdefault(_state_key(api_base, model), asyncio.Lock())


def reset_state() -> None:
    _SLOT_STATE.clear()
    _LOCKS.clear()


def _slug(text: str) -> str:
    out = []
    for ch in text:
        out.append(ch if ch.isalnum() else "-")
    return "".join(out)[:80]


# ---------------------------------------------------------------------------
# Ledger (bin registry under ~/.yaah/slotcache/<api>/<model>.json)
# ---------------------------------------------------------------------------


def _ledger_path(api_base: str, model: str) -> Path:
    from backend.agent.config import CONFIG_PATH

    root = Path(CONFIG_PATH).parent  # ~/.yaah (or the test redirect)
    return root / "slotcache" / _slug(api_base) / f"{_slug(model)}.json"


def read_ledger(api_base: str, model: str) -> dict:
    try:
        data = json.loads(_ledger_path(api_base, model).read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_ledger(api_base: str, model: str, ledger: dict) -> None:
    path = _ledger_path(api_base, model)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace (issue #557 review): a torn in-place write would
        # read back as {} on next boot, orphaning every bin in the ledger.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(ledger, indent=1), "utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("slotcache: ledger write failed: %s", e)


def hash10(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def base_bin_name(api_base: str, model: str, system_prompt: str) -> str:
    return f"sys-{hash10(api_base + model + system_prompt)}.bin"


def conv_bin_name(conversation_id: int) -> str:
    return f"conv-{int(conversation_id)}.bin"


def _bin_entry(ledger: dict, name: str) -> dict | None:
    for entry in ledger.get("bins", []):
        if entry.get("file") == name:
            return entry
    return None


def _upsert_bin(ledger: dict, entry: dict) -> dict:
    bins = [b for b in ledger.get("bins", []) if b.get("file") != entry["file"]]
    bins.append(entry)
    bins.sort(key=lambda b: b.get("ts") or 0)
    ledger["bins"] = bins
    return ledger


# ---------------------------------------------------------------------------
# Router probes (return None / raise _Degraded on any doubt)
# ---------------------------------------------------------------------------


class _Degraded(Exception):
    """Any condition where the only safe move is to skip the cache."""


def _router_base(api_base: str) -> str:
    # http://host:8080/v1 -> http://host:8080 (the router's own surface)
    return api_base.rstrip("/").removesuffix("/v1")


async def _get_json(base: str, path: str, timeout: float):
    import httpx as _hx

    async with _hx.AsyncClient() as client:
        r = await client.get(base + path, timeout=timeout)
    if r.status_code != 200:
        raise _Degraded(f"HTTP {r.status_code} for {path}")
    return r.json()


async def _post_action(base: str, id_slot: int, action: str, model: str, filename: str = "") -> dict:
    import httpx as _hx

    async with _hx.AsyncClient() as client:
        r = await client.post(
            f"{base}/slots/{id_slot}?action={action}",
            json={"model": model, "filename": filename},
            timeout=30.0,
        )
    if r.status_code != 200:
        raise _Degraded(f"HTTP {r.status_code} for {action}")
    data = r.json()
    if not isinstance(data, dict):
        raise _Degraded(f"unexpected {action} payload")
    return data


async def _probe(api_base: str, preset: str):
    """/v1/models + /slots, with the unloaded-model hang avoidance: the
    router's /slots never autoloads and HANGS on an unloaded preset, so
    /v1/models is consulted first and an unloaded model degrades before
    the /slots call (issue #557 item 4)."""
    # The ROUTER surface, not the OpenAI-style api_base: /v1/models and
    # /slots live at the root, so an api_base of http://host:8080/v1 must
    # not be prepended again (round-3 review: the doubled /v1/v1/ prefix
    # 404s and silently disables the whole module).
    base = _router_base(api_base)
    try:
        models = await _get_json(base, "/v1/models", 5.0)
    except _Degraded:
        raise
    except Exception as e:  # noqa: BLE001
        raise _Degraded(f"models probe failed: {e}") from e
    entry = next(
        (
            m
            for m in (models.get("data") or [])
            if isinstance(m, dict) and m.get("id") == preset
        ),
        None,
    )
    if entry is None:
        return None
    status = entry.get("status") or {}
    if status.get("value") != "loaded":
        # Do NOT touch /slots here: on this router it hangs instead of
        # erroring, and the caller would rather proceed ungated.
        log.info("slotcache: %s not loaded (%s), skipping cache", preset, status.get("value"))
        return None
    try:
        snapshot = await _get_json(base, f"/slots?model={preset}", 3.0)
    except Exception as e:  # noqa: BLE001
        raise _Degraded(f"slots probe failed: {e}") from e
    if not isinstance(snapshot, list) or not snapshot:
        return None
    slot = snapshot[0]
    if not isinstance(slot, dict) or slot.get("is_processing"):
        return None
    return entry, slot


def boot_fingerprint(models_entry: dict) -> str:
    """Child identity: hash of the spawn args from /v1/models status - the
    only per-child signal the router exposes (no pid; issue #557 item 7).
    Weak in isolation (cold presets carry template args), but we only
    fingerprint LOADED children, whose args include the real spawned port;
    combined with id_task equality and a conservative default it is the
    best available restart signal."""
    args = (models_entry.get("status") or {}).get("args") or []
    return hash10(json.dumps(args))


# ---------------------------------------------------------------------------
# Decision core (pure - unit-testable without HTTP)
# ---------------------------------------------------------------------------


def decide(
    slot: dict | None,
    recorded: dict | None,
    boot_now: str,
    ledger: dict,
    conversation_id: int,
    is_new_conversation: bool,
    base_name: str,
) -> dict:
    """Pure gate decision: what should happen before this turn's first
    model call.

    Returns {"action": "skip"|"restore"|"bootstrap", "bin": str|None,
    "reason": str}.
    HOT = same child (boot fingerprint) AND id_task unchanged since the
    recorded post-save observation AND that observation was for THIS
    conversation under the CURRENT prompt head (prompt_key) AND the turn
    continues an existing conversation. A new conversation is never "hot"
    (its prefix has no shared tail to protect beyond the BASE) and
    restores the BASE bin when one exists, or asks for a bootstrap when
    the persona has none. An existing conversation restores its own
    conv bin, falling back to the BASE.
    """
    if slot is None:
        return {"action": "skip", "bin": None, "reason": "no-slot-info"}
    id_task = slot.get("id_task")
    if (
        recorded
        and recorded.get("boot") == boot_now
        and recorded.get("conversation_id") == conversation_id
        and recorded.get("prompt_key") == base_name
        and recorded.get("id_task") is not None
        and id_task is not None
        and int(recorded["id_task"]) == int(id_task)
        and not is_new_conversation
    ):
        return {"action": "skip", "bin": None, "reason": "hot"}
    base_entry = _bin_entry(ledger, base_name)
    if is_new_conversation:
        if base_entry:
            return {"action": "restore", "bin": base_name, "reason": "seed"}
        return {"action": "bootstrap", "bin": base_name, "reason": "need-base"}
    # A conv bin is only eligible while its prompt head still matches
    # (issue #557 review round 1): a bin baked under an old system prompt
    # must never be restored under a rebuilt one.
    conv_entry = _bin_entry(ledger, conv_bin_name(conversation_id))
    if conv_entry and conv_entry.get("prompt_key") == base_name:
        return {
            "action": "restore",
            "bin": conv_entry.get("file"),
            "reason": "conv-bin",
        }
    if base_entry:
        return {"action": "restore", "bin": base_name, "reason": "base-fallback"}
    # No checkpoint of any kind: bootstrapping the persona BASE also warms
    # this slot for the turn (the aux prefill replaces whatever stale
    # content the slot holds - its [system] prefix survives either way).
    return {"action": "bootstrap", "bin": base_name, "reason": "need-base"}


# ---------------------------------------------------------------------------
# Public API (called from loop.py)
# ---------------------------------------------------------------------------


def resolve_preset(cfg: dict) -> str | None:
    """The exact preset id the ROUTER knows (its /slots rejects anything
    else - the stored chat id can be an alias form; issue #557 item 9).
    Hard gate: llama.cpp's conventional port. Hosted providers never
    match, and the whole module is a no-op for them."""
    base = (cfg.get("api_base") or "").rstrip("/")
    model = cfg.get("model") or ""
    if not base or not model:
        return None
    host = base.split("//", 1)[-1]
    if not (host.endswith(":8080") or ":8080/" in base):
        return None
    return model


def cached_tokens(usage: dict | None) -> int | None:
    """Cached-prefix token count from an OpenAI-style usage blob.
    llama.cpp fills prompt_tokens_details.cached_tokens (verified live on
    blocking and streamed responses)."""
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), (int, float)):
        return int(details["cached_tokens"])
    return None


async def gate(
    model: str = "",
    effort: str | None = "",
    conversation_id: int = 0,
    is_new_conversation: bool = False,
    system_prompt: str = "",
    cancel_ev: asyncio.Event | None = None,
) -> None:
    """Turn-start probe/restore/bootstrap. Best-effort by contract: never
    raises; any degrade means the turn proceeds exactly as without this
    module. Must only be called BEFORE the turn's first model call (never
    mid-run). cancel_ev (when given) aborts a BASE bootstrap mid-prefill
    the moment the user hits Stop."""
    try:
        await _gate_inner(
            model, effort, conversation_id, is_new_conversation, system_prompt,
            cancel_ev,
        )
    except Exception as e:  # noqa: BLE001 - degrade silently, always
        log.warning("slotcache: gate degraded: %s", e)


async def _gate_inner(
    model: str,
    effort: str | None,
    conversation_id: int,
    is_new_conversation: bool,
    system_prompt: str,
    cancel_ev: asyncio.Event | None = None,
) -> None:
    from backend.agent import model_client
    from backend.agent.config import load_config

    cfg = model_client._resolve_call_cfg(dict(load_config()), model, effort)
    preset = resolve_preset(cfg)
    if not preset or not system_prompt:
        return
    api_base = cfg["api_base"].rstrip("/")
    key = _state_key(api_base, preset)
    lock = slot_lock(api_base, preset)
    if lock.locked():
        # Another caller (sibling chat) is mid-probe/restore on this
        # model. Rather than queue (and rather than disturb), proceed
        # ungated: the server still prefix-matches whatever it holds.
        log.info("slotcache: %s gate busy, proceeding ungated", preset)
        return
    async with lock:
        probed = await _probe(api_base, preset)
        if probed is None:
            return
        models_entry, slot = probed
        boot = boot_fingerprint(models_entry)
        ledger = read_ledger(api_base, preset)
        base_name = base_bin_name(api_base, preset, system_prompt)
        decision = decide(
            slot,
            _SLOT_STATE.get(key),
            boot,
            ledger,
            conversation_id,
            is_new_conversation,
            base_name,
        )
        if decision["action"] == "restore":
            try:
                await _post_action(
                    _router_base(api_base), 0, "restore", preset,
                    filename=decision["bin"],
                )
            except (_Degraded, httpx.HTTPError) as e:
                log.warning("slotcache: restore %s failed: %s", decision["bin"], e)
                return
            # Record the post-restore observation: saves/restores bump id_task
            # themselves, so the recorded value must be read back afterwards.
            recorded = await _record_post_task(
                api_base, preset, conversation_id, base_name
            )
            log.info(
                "slotcache: conv %s restored %s (%s)%s",
                conversation_id,
                decision["bin"],
                decision["reason"],
                f" id_task={recorded['id_task']}" if recorded else "",
            )
            return
        if decision["action"] == "bootstrap":
            # Persona BASE missing: bank the system prompt NOW, at the
            # start of the first message that needs it - independent of
            # whether this run ever finishes (issue #557 spec: the BASE
            # exists once prefill is done, crash/Stop notwithstanding).
            if len(system_prompt or "") < BASE_MIN_SYSTEM_CHARS:
                _SLOT_STATE[key] = {
                    "boot": boot,
                    "conversation_id": conversation_id,
                    "id_task": slot.get("id_task"),
                    "prompt_key": None,
                }
                return
            try:
                # Aux prefill of EXACTLY the system prompt. The chat
                # template renders it byte-identically to a real call's
                # head, so the saved BASE prefix-matches every future
                # conversation on this persona; at worst a divergence past
                # the system block is re-prefilled by the server's normal
                # prefix matching.
                from backend.agent.loop import _race_model_call

                # max_tokens=1 bounds generation (a reasoning model could
                # otherwise think for a long while under the slot lock -
                # round-3 review); the PREFILL is what banks the BASE and
                # it completes before any generation regardless. Raced
                # against the turn's cancel event: Stop wins immediately
                # and tears the request down (issue #389 semantics).
                call = asyncio.create_task(
                    model_client.chat(
                        messages=[{"role": "system", "content": system_prompt}],
                        tools=None,
                        stream=False,
                        model=model,
                        effort=effort,
                        max_tokens=1,
                    )
                )
                await _race_model_call(call, cancel_ev)
            except model_client.ModelCancelled:
                log.info("slotcache: base bootstrap %s cancelled", base_name)
                return
            except Exception as e:  # noqa: BLE001 - degrade, never raise
                log.warning(
                    "slotcache: base prefill %s failed: %s", base_name, e
                )
                return
            try:
                save_resp = await _post_action(
                    _router_base(api_base), 0, "save", preset, filename=base_name
                )
            except (_Degraded, httpx.HTTPError) as e:
                log.warning("slotcache: base save %s failed: %s", base_name, e)
                return
            if int(save_resp.get("n_saved") or 0) <= 0:
                log.info("slotcache: base save %s reported 0 tokens", base_name)
                return
            ledger = read_ledger(api_base, preset)
            ledger = _upsert_bin(
                ledger,
                {
                    "file": base_name,
                    "kind": "base",
                    "n_tokens": int(save_resp.get("n_saved") or 0),
                    "n_written": int(save_resp.get("n_written") or 0),
                    "boot": boot,
                    "id_task_at_save": slot.get("id_task"),
                    "prompt_key": base_name,
                    "ts": time.time(),
                },
            )
            _write_ledger(api_base, preset, ledger)
            recorded = await _record_post_task(
                api_base, preset, conversation_id, base_name
            )
            log.info(
                "slotcache: base %s bootstrapped (%d tokens) for conv %s%s",
                base_name,
                int(save_resp.get("n_saved") or 0),
                conversation_id,
                f" id_task={recorded['id_task']}" if recorded else "",
            )
            return
        # Plain skip. The probe succeeded: record the observation even on
        # skip, so (a) the turn-end flush knows this conversation went
        # through the gate (its precondition), and (b) the fingerprint is
        # fresh for the next turn's hot-check. Only a hot decision extends
        # hotness under this prompt head; any other skip must not.
        _SLOT_STATE[key] = {
            "boot": boot,
            "conversation_id": conversation_id,
            "id_task": slot.get("id_task"),
            "prompt_key": base_name if decision["reason"] == "hot" else None,
        }
        log.info(
            "slotcache: conv %s gate=%s (%s)",
            conversation_id, decision["action"], decision["reason"],
        )


async def _record_post_task(
    api_base: str, preset: str, conversation_id: int, prompt_key: str | None
) -> dict | None:
    """Re-probe after a save/restore/prefill and store the volatile
    fingerprint for this conversation. Best-effort: a failed re-probe
    leaves no record, so the next gate treats the slot as cold
    (conservative). prompt_key pins the record to the prompt head it was
    built from (issue #557 review)."""
    probed = await _probe(api_base, preset)
    if probed is None:
        _SLOT_STATE.pop(_state_key(api_base, preset), None)
        return None
    models_entry, slot = probed
    record = {
        "boot": boot_fingerprint(models_entry),
        "conversation_id": conversation_id,
        "id_task": slot.get("id_task"),
        "prompt_key": prompt_key,
    }
    _SLOT_STATE[_state_key(api_base, preset)] = record
    return record


async def flush(
    model: str = "",
    effort: str | None = "",
    conversation_id: int = 0,
    system_prompt: str = "",
    usage: dict | None = None,
) -> None:
    """Turn-end checkpoint: save the slot (holding the just-finished turn's
    prefix) as this conversation's ONE bin, overwriting the previous one.
    Best-effort, never raises. Call ONLY after the turn's model activity
    is done (same rule as gate: never while a completion is in flight)."""
    try:
        await _flush_inner(model, effort, conversation_id, system_prompt, usage)
    except Exception as e:  # noqa: BLE001 - degrade silently, always
        log.warning("slotcache: flush degraded: %s", e)


async def _flush_inner(
    model: str,
    effort: str | None,
    conversation_id: int,
    system_prompt: str,
    usage: dict | None,
) -> None:
    from backend.agent import model_client
    from backend.agent.config import load_config

    cfg = model_client._resolve_call_cfg(dict(load_config()), model, effort)
    preset = resolve_preset(cfg)
    if not preset:
        return
    api_base = cfg["api_base"].rstrip("/")
    key = _state_key(api_base, preset)
    if (_SLOT_STATE.get(key) or {}).get("conversation_id") != conversation_id:
        # This conversation never gated on this slot (subagent turn, busy
        # skip, other conversation owns the fingerprint) - nothing to save.
        return
    cached = cached_tokens(usage)
    if cached is not None:
        log.info(
            "slotcache: conv %s last call cached_tokens=%s",
            conversation_id, cached,
        )
    lock = slot_lock(api_base, preset)
    async with lock:
        # Re-check ownership INSIDE the lock (issue #557 review): a sibling
        # gate can claim the slot while this flush waits for the lock; the
        # pre-lock check above is then stale. Re-read fresh.
        if (_SLOT_STATE.get(key) or {}).get("conversation_id") != conversation_id:
            return
        probed = await _probe(api_base, preset)
        if probed is None:
            return
        models_entry, slot = probed
        boot = boot_fingerprint(models_entry)
        ledger = read_ledger(api_base, preset)
        bin_name = conv_bin_name(conversation_id)
        base_name = base_bin_name(api_base, preset, system_prompt)
        router_base = _router_base(api_base)
        try:
            save_resp = await _post_action(
                router_base, 0, "save", preset, filename=bin_name
            )
        except (_Degraded, httpx.HTTPError) as e:
            log.warning("slotcache: save %s failed: %s", bin_name, e)
            return
        n_saved = int(save_resp.get("n_saved") or 0)
        if n_saved <= 0:
            log.info("slotcache: save %s reported 0 tokens, not recorded", bin_name)
            return
        entry = {
            "file": bin_name,
            "kind": "turn",
            "conversation_id": conversation_id,
            "n_tokens": n_saved,
            "n_written": int(save_resp.get("n_written") or 0),
            "boot": boot,
            "id_task_at_save": slot.get("id_task"),
            "prompt_key": base_name,
            "ts": time.time(),
        }
        ledger = _upsert_bin(ledger, entry)
        _write_ledger(api_base, preset, ledger)
        recorded = await _record_post_task(
            api_base, preset, conversation_id, base_name
        )
        log.info(
            "slotcache: saved %s (%d tokens, %d bytes) for conv %s%s",
            bin_name,
            n_saved,
            entry["n_written"],
            conversation_id,
            f" id_task={recorded['id_task']}" if recorded else "",
        )
