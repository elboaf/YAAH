"""Disk-backed prompt cache via llama.cpp slot save/restore (issue #557).

When the active provider is a llama.cpp-class server with the slots API
enabled (``--slots --slot-save-path``), the harness backs its prompt cache
with disk files so that:

- New conversations on the same model + frozen system prompt start from a
  cached system-prefix bin ("BASE"/seed bin) instead of re-prefilling it.
- A conversation resumes from disk after the hot (RAM) slot is evicted
  (child swap, eviction, service restart).

Contract (verified live against the herp router 2026-10-25 - see the
issue): GET endpoints take ``?model=<preset>``; save/restore/erase POSTs
carry ``model`` in the JSON body; ``id_task`` is absent on a fresh child
and bumps with EVERY server task (completions, saves, restores); there is
no pid and no file-delete API; a ``/slots`` probe against an unloaded
preset HANGS, so availability is gated on ``/v1/models``
``status.value == "loaded"`` first. Everything degrades silently: any
error, 404, or timeout means the completion proceeds exactly as without
this module.

Bookkeeping rules that fall out of the live probes (each one bit during
design):

- ``id_task`` is a monotonic task counter, NOT a content indicator. The
  "slot untouched since my save" fingerprint must therefore be observed
  AFTER the save/restore (saves themselves bump the counter), and it is
  valid only while no other task ran on the slot.
- The gate runs ONLY at the first model step of a turn. Mid-turn the slot
  legitimately holds newer state than any checkpoint (this turn's earlier
  steps); restoring there would DESTROY that fresher tail. Mid-turn turns
  run exactly as today - the server's own prompt-prefix cache does the
  work.
- Hotness is per-conversation runtime state: HOT iff the same child
  (boot fingerprint) AND the recorded task-id matches AND the recorded
  save belonged to THIS conversation. Anything else restores the newest
  bin - conservative on purpose (restore is sub-second; a cold re-prefill
  is minutes).
- After a harness restart hotness is UNKNOWN -> conservative restore.

Design:
  seed/BASE bin  ``sys-<hash10>.bin``  key = sha1(api_base + preset + system)
                 seeded from a conversation's own save when the key is new
                 (content = system + first-turn tail; prefix-matching makes
                 the extra tail harmless - a new conversation matches only
                 the system head it shares).
  turn bins      ``conv-<id>-t<turn>.bin``  saved after each completed turn.
  ledger         harness-side JSON per model; the source of truth for what
                 is on disk. Bin content is opaque and never parsed.

Mid-run checkpointing (the 4-8K-token boundary saves of the original
design) is intentionally NOT implemented: the save needs the slot mutex
and would serialize sibling chats against a still-streaming run, and a
mid-run gate cannot distinguish "my fresher tail" from "another
conversation's prefix" (both just bump id_task). Turn-end flush covers the
acceptance criteria; revisit behind the same flush helper.
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

log = logging.getLogger("yaah.slotcache")

# ---------------------------------------------------------------------------
# Timeouts: every slots-API call is tightly bounded. A cold/unresponsive
# router HANGS these endpoints (verified live) - the gate must never add
# more than a couple of seconds to a turn, and never wait on a model load
# (the loaded-check on /v1/models runs first for exactly that reason).
# ---------------------------------------------------------------------------
_MODELS_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=2.0)
_SLOTS_GET_TIMEOUT = httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)
# Saves/restores move up to gigabytes on the router's tmpfs; generous read
# budget, still bounded.
_SLOTS_ACTION_TIMEOUT = httpx.Timeout(connect=2.0, read=30.0, write=5.0, pool=2.0)

# Keep at most this many turn bins per conversation (newest kept), and cap
# their total size. The per-bin fixed cost is large (~124 MB on the
# 131k-ctx preset - issue #557 item 8), so the ledger's n_written drives a
# real byte budget, not a token estimate.
MAX_BINS_PER_CONVERSATION = 4
MAX_BYTES_PER_CONVERSATION = 800 * 1024 * 1024

# A system prefix below this size is not worth a seed bin (the warm-up
# cost amortizes to nothing).
BASE_MIN_SYSTEM_CHARS = 1024

# The slot mutex belongs to the MODEL (each preset = exactly one child with
# ONE slot, --parallel 1), not to this process: sibling chats share the
# model, so they must share the mutex. Cross-process locking is out of
# scope - only one harness drives the router today.
_SLOT_LOCKS: dict[str, asyncio.Lock] = {}

# Runtime hotness state per (api_base, preset):
#   {"boot": <args-hash>, "conv": <conversation_id>, "id_task": <int>}
# recorded immediately AFTER our last save/restore for that conversation.
# The ledger is the durable record; this is the volatile fingerprint.
_SLOT_STATE: dict[str, dict] = {}


class _Degraded(Exception):
    """Slots feature unavailable/uncertain - the caller degrades silently."""


def _state_key(api_base: str, model: str) -> str:
    return f"{api_base}>{model}"


def reset_state() -> None:
    """Drop volatile fingerprints/locks. Test hook; also the documented
    reaction to a router restart at runtime (fingerprints are never
    trusted across a restart)."""
    _SLOT_STATE.clear()
    _SLOT_LOCKS.clear()


def slot_lock(api_base: str, model: str) -> asyncio.Lock:
    """The per-model mutex serializing probe/restore/save for a preset."""
    key = _state_key(api_base, model)
    if key not in _SLOT_LOCKS:
        _SLOT_LOCKS[key] = asyncio.Lock()
    return _SLOT_LOCKS[key]


# ---------------------------------------------------------------------------
# Ledger: one JSON file per (api_base, preset) under ~/.yaah/slotcache/.
# The ledger - not the server - is the source of truth for what is on disk.
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in text)[:80]


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


def turn_bin_name(conversation_id: int, turn: int) -> str:
    return f"conv-{int(conversation_id)}-t{int(turn)}.bin"


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


def _newest_turn_bins(ledger: dict, conversation_id: int) -> list[dict]:
    bins = [
        e
        for e in ledger.get("bins", [])
        if e.get("kind") == "turn" and e.get("conversation_id") == conversation_id
    ]
    bins.sort(key=lambda e: e.get("turn") or 0)
    return bins


def next_turn_number(ledger: dict, conversation_id: int) -> int:
    """One past the highest persisted turn bin for this conversation. Taken
    from the ledger (not process state) so bin names stay unique across
    harness restarts."""
    turns = _newest_turn_bins(ledger, conversation_id)
    return (turns[-1].get("turn") or 0) + 1 if turns else 1


def _evict_turn_bins(ledger: dict, conversation_id: int) -> list[str]:
    """Enforce the per-conversation count/byte budgets, evicting the OLDEST
    turn bins. Returns the evicted filenames. Bin DELETION is ledger-only
    today: the slot-save-path lives on the ROUTER's tmpfs and there is no
    delete API (issue #557 open question 1 - an ops decision), so eviction
    trims the ledger and the budget guard stops unbounded growth. A
    router-side janitor can garbage-collect orphaned .bin files by mtime;
    the ledger tolerates already-missing files. The NEWEST bin is never
    evicted: dropping it would leave a cold conversation with no restore
    point (and restart its turn numbering), so the ledger may sit over
    budget by one bin (issue #557 review)."""
    bins = _newest_turn_bins(ledger, conversation_id)
    over_count = len(bins) - MAX_BINS_PER_CONVERSATION
    over_bytes = (
        sum(int(b.get("n_written") or 0) for b in bins) - MAX_BYTES_PER_CONVERSATION
    )
    evicted: list[str] = []
    for entry in bins[:-1]:  # oldest first; the NEWEST bin is never evicted
        if over_count <= 0 and over_bytes <= 0:
            break
        if over_count >= 0:
            over_count -= 1
        if over_bytes > 0:
            over_bytes -= int(entry.get("n_written") or 0)
        evicted.append(entry.get("file") or "")
        ledger["bins"] = [b for b in ledger["bins"] if b.get("file") != entry.get("file")]
    return [f for f in evicted if f]


# ---------------------------------------------------------------------------
# Server probes (return None / raise _Degraded on any doubt)
# ---------------------------------------------------------------------------


def _router_base(api_base: str) -> str:
    base = api_base.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


async def _get_json(base: str, path: str, timeout: httpx.Timeout) -> object:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{base}{path}")
    if r.status_code != 200:
        raise _Degraded(f"HTTP {r.status_code} for {path}")
    return r.json()


async def _post_action(
    base: str, id_slot: int, action: str, model: str, filename: str = ""
) -> dict:
    """POST /slots/{id}?action=<act> with the model in the BODY (verified
    live; the query-param form is also accepted but the body is what the
    router documents-by-behavior)."""
    body: dict = {"model": model}
    if filename:
        body["filename"] = filename
    async with httpx.AsyncClient(timeout=_SLOTS_ACTION_TIMEOUT) as client:
        r = await client.post(f"{base}/slots/{id_slot}?action={action}", json=body)
    if r.status_code != 200:
        raise _Degraded(f"HTTP {r.status_code} for action={action}")
    data = r.json()
    if not isinstance(data, dict):
        raise _Degraded(f"unexpected {action} payload")
    return data


async def _probe(api_base: str, preset: str) -> tuple[dict, dict] | None:
    """(models_entry, slot_snapshot) for a loaded preset's slot 0, or
    None = degrade.

    Order matters (verified live): /v1/models status FIRST - a /slots
    probe against an UNLOADED preset HANGS until read-timeout, and the
    last thing a pre-send gate should do is add a stall to every turn. A
    busy slot also degrades: the gate must not delay (or disturb) a
    running completion.
    """
    base = _router_base(api_base)
    try:
        models = await _get_json(base, "/v1/models", _MODELS_TIMEOUT)
        if not isinstance(models, dict):
            raise _Degraded("bad /v1/models payload")
        entry = None
        for m in models.get("data", []):
            if isinstance(m, dict) and m.get("id") == preset:
                entry = m
                break
        if entry is None or (entry.get("status") or {}).get("value") != "loaded":
            raise _Degraded(f"{preset} not loaded")
        snapshot = await _get_json(base, f"/slots?model={preset}", _SLOTS_GET_TIMEOUT)
    except (_Degraded, httpx.HTTPError, ValueError):
        return None
    if not (isinstance(snapshot, list) and snapshot):
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

    Returns {"action": "skip"|"restore", "bin": str|None, "reason": str}.
    HOT = same child (boot fingerprint) AND id_task unchanged since the
    recorded post-save observation AND that observation was for THIS
    conversation under the CURRENT prompt head (prompt_key) AND the turn
    continues an existing conversation. A new
    conversation is never "hot" (its prefix has no shared tail to protect
    beyond the seed) and restores the seed bin when one exists.
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
    if is_new_conversation:
        entry = _bin_entry(ledger, base_name)
        if entry:
            return {"action": "restore", "bin": base_name, "reason": "seed"}
        return {"action": "skip", "bin": None, "reason": "no-seed-yet"}
    # Turn bins are only eligible while their prompt head still matches
    # (issue #557 review): a rebuilt system prompt would otherwise
    # cold-restore a checkpoint baked from the old head.
    turns = [
        e
        for e in _newest_turn_bins(ledger, conversation_id)
        if e.get("prompt_key") == base_name
    ]
    if turns:
        return {"action": "restore", "bin": turns[-1]["file"], "reason": "cold"}
    # Existing conversation whose bins are all gone (evicted/other harness):
    # fall back to the seed bin - better than nothing, prefix-match decides.
    entry = _bin_entry(ledger, base_name)
    if entry:
        return {"action": "restore", "bin": base_name, "reason": "seed-fallback"}
    return {"action": "skip", "bin": None, "reason": "no-bin"}


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
) -> None:
    """Turn-start probe/restore. Best-effort by contract: never raises; any
    degrade means the turn proceeds exactly as without this module. Must
    only be called BEFORE the turn's first model call (never mid-run)."""
    try:
        await _gate_inner(
            model, effort, conversation_id, is_new_conversation, system_prompt
        )
    except Exception as e:  # noqa: BLE001 - degrade silently, always
        log.warning("slotcache: gate degraded: %s", e)


async def _gate_inner(
    model: str,
    effort: str | None,
    conversation_id: int,
    is_new_conversation: bool,
    system_prompt: str,
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
        if decision["action"] != "restore":
            # The probe succeeded: record the observation even on skip, so
            # (a) the turn-end flush knows this conversation went through
            # the gate (its precondition), and (b) the fingerprint is fresh
            # for the next turn's hot-check. id_task here is the observed
            # value; the flush's post-save re-observation supersedes it.
            _SLOT_STATE[key] = {
                "boot": boot,
                "conversation_id": conversation_id,
                "id_task": slot.get("id_task"),
                # Only a hot decision extends hotness under this prompt
                # head; any other skip must not (issue #557 review).
                "prompt_key": base_name if decision["reason"] == "hot" else None,
            }
            log.info(
                "slotcache: conv %s gate=%s (%s)",
                conversation_id, decision["action"], decision["reason"],
            )
            return
        # ROUND6: a restore onto a slot holding ANY prior content
        # (warm-up prefill, previous conversation) silently poisons
        # the server's prefix matcher - restored tokens match 0 and
        # the turn re-prefills everything (live-verified 2026-10-25:
        # erase-then-restore -> 7580/7597 cached, 1 s; restore over
        # prior content -> 0/7591 cached, ~70 s). Erase first: the
        # bin we restore is the full content the slot needs.
        try:
            await _post_action(_router_base(api_base), 0, "erase", preset)
        except (_Degraded, httpx.HTTPError) as e:
            log.warning("slotcache: pre-restore erase failed: %s", e)
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


async def _record_post_task(
    api_base: str, preset: str, conversation_id: int, prompt_key: str | None
) -> dict | None:
    """Re-probe after a save/restore and store the volatile fingerprint for
    this conversation. Best-effort: a failed re-probe leaves no record, so
    the next gate restores again (conservative). prompt_key pins the record
    to the prompt head it was built from (issue #557 review)."""
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
    is_first_turn: bool = False,
) -> None:
    """Turn-end checkpoint: save the slot (holding the just-finished turn's
    prefix) as this conversation's newest turn bin, and - only on a first
    turn (is_first_turn, computed in loop.py) - as the persona's seed/BASE
    bin when that key has no bin yet. Best-effort, never raises.
    Call ONLY after the turn's model activity is done (same rule as gate:
    never while a completion is in flight)."""
    try:
        await _flush_inner(
            model, effort, conversation_id, system_prompt, usage, is_first_turn
        )
    except Exception as e:  # noqa: BLE001 - degrade silently, always
        log.warning("slotcache: flush degraded: %s", e)


async def _flush_inner(
    model: str,
    effort: str | None,
    conversation_id: int,
    system_prompt: str,
    usage: dict | None,
    is_first_turn: bool,
) -> None:
    from backend.agent import model_client
    from backend.agent.config import load_config

    cfg = model_client._resolve_call_cfg(dict(load_config()), model, effort)
    preset = resolve_preset(cfg)
    if not preset:
        return
    api_base = cfg["api_base"].rstrip("/")
    key = _state_key(api_base, preset)
    state = _SLOT_STATE.get(key) or {}
    if state.get("conversation_id") != conversation_id:
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
        # pre-lock `state` above is then stale. Re-read fresh, not `state`.
        if (_SLOT_STATE.get(key) or {}).get("conversation_id") != conversation_id:
            return
        probed = await _probe(api_base, preset)
        if probed is None:
            return
        models_entry, slot = probed
        boot = boot_fingerprint(models_entry)
        ledger = read_ledger(api_base, preset)
        turn = next_turn_number(ledger, conversation_id)
        bin_name = turn_bin_name(conversation_id, turn)
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
            "turn": turn,
            "n_tokens": n_saved,
            "n_written": int(save_resp.get("n_written") or 0),
            "boot": boot,
            "id_task_at_save": slot.get("id_task"),
            "prompt_key": base_name,
            "ts": time.time(),
        }
        ledger = _upsert_bin(ledger, entry)
        # Seed/BASE bin for this persona: only a conversation's FIRST turn
        # may create it (a pre-cache conversation's first flush would bake
        # its whole long prefix into the seed - issue #557 review), and
        # only if the system key has no bin yet. One extra save (~tens of
        # ms) amortizes across every future conversation on this persona.
        if is_first_turn and len(system_prompt or "") >= BASE_MIN_SYSTEM_CHARS:
            if not _bin_entry(ledger, base_name):
                try:
                    base_resp = await _post_action(
                        router_base, 0, "save", preset, filename=base_name
                    )
                except (_Degraded, httpx.HTTPError) as e:
                    log.warning("slotcache: seed save %s failed: %s", base_name, e)
                else:
                    if int(base_resp.get("n_saved") or 0) > 0:
                        ledger = _upsert_bin(
                            ledger,
                            {
                                "file": base_name,
                                "kind": "base",
                                "n_tokens": int(base_resp.get("n_saved") or 0),
                                "n_written": int(base_resp.get("n_written") or 0),
                                "boot": boot,
                                "id_task_at_save": slot.get("id_task"),
                                "ts": time.time(),
                            },
                        )
                        log.info("slotcache: seed bin %s saved", base_name)
        for evicted in _evict_turn_bins(ledger, conversation_id):
            log.info("slotcache: evicted %s from ledger (budget)", evicted)
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
