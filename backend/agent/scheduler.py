"""Scheduled agents (issue #41): first-class, user-scheduled recurring runs.

Agents live in the SQLite `agents` table (durable — the Tauri supervisor
respawns the backend on crash, and schedules must survive that), each with a
pinned conversation of chat_type='agent'. The scheduler is an asyncio task
in the FastAPI lifespan: it ticks periodically, computes due agents from the
database, and fires each as a turn via run_agent.

Spec decisions implemented here:
- First fire is never immediate: it lands after the first interval / at the
  next clock slot ("Run now" in the dialogue covers testing).
- Missed fires while no backend was alive are SKIPPED silently — no
  catch-up on launch; overdue next_fire_at values roll forward at boot.
- Failed fires retry per the GLOBAL retry setting (config "agents":
  retry_count / retry_backoff_minutes), not per-agent.
- Runs fire in parallel with the user's turn and each other, unlimited.
  The one guard that remains is per-conversation: a fire landing while that
  same chat is mid-turn is postponed briefly rather than dropped on the
  run-lock error (the spec's accepted collision risk is about the shared
  working tree, not double-firing one chat).
"""
import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta

from backend.agent.config import load_config, save_config
from backend.db.database import (
    get_agent,
    get_conversation,
    list_agents,
    list_instructions,
    trim_agent_transcript,
    update_agent as _db_update_agent,
)


def _patch(agent_id: str, **fields) -> dict | None:
    """Merge-update one agent row (dict-form wrapper over the DB layer)."""
    return _db_update_agent(agent_id, fields)

log = logging.getLogger("yaah.scheduler")

VALID_SCHEDULE_TYPES = ("interval", "daily", "weekly")
VALID_POLICIES = ("sandbox-only", "autonomous")
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Interval bounds: below this would hammer the model provider; above a
# month makes "next fire" effectively meaningless.
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 60 * 24 * 30

# How often the scheduler wakes up to look for due agents.
TICK_SECONDS = 30

# When an agent's conversation is mid-turn at fire time: retry after this
# long instead of losing the run to the per-conversation run lock.
BUSY_RETRY_SECONDS = 60

_task: asyncio.Task | None = None

# Per-fire retry bookkeeping (in-memory on purpose: a restart resets retry
# attempts, which matches "the schedule itself survives, attempts don't").
# agent_id -> {"attempts": int, "scheduled_next": iso}
_retry_state: dict[str, dict] = {}

# Strong refs to the run tasks. The event loop holds only weak refs to
# tasks, so an unreferenced create_task can be garbage-collected mid-run
# at its next await — the run silently vanishes (no error, no finally),
# leaving last_status="running" forever. This set is what keeps them alive.
_fire_tasks: set[asyncio.Task] = set()

# UI stream events of the run currently executing in each conversation,
# so the open agent chat can show the live telemetry tape (a scheduled
# run never flows through the frontend's stream handler). Process-local
# on purpose: runs live in this process, like _retry_state. Each entry
# is {"seq": int, "running": bool, "events": [trimmed event dicts]} —
# seq is the total appended so far (the frontend polls with `after`).
# The buffer resets at the start of each fire.
TAPE_EVENT_CAP = 400
_tape_buffers: dict[int, dict] = {}

_TAPE_FIELDS = (
    "type", "text", "name", "command", "chunk", "result", "message",
    # #93: ask_user's payload — without args the open chat cannot render the
    # question card, without call_id the answer cannot be routed back.
    "args", "call_id",
)
_TAPE_VALUE_CAP = 2000


def tape_snapshot(conv_id: int, after: int = 0) -> dict:
    """Events appended to conv_id's buffer after index `after`, plus the
    run state. `offset` lets the caller resume without re-fetching."""
    buf = _tape_buffers.get(conv_id)
    if buf is None:
        return {"running": False, "offset": after, "events": []}
    return {
        "running": buf["running"],
        "offset": buf["seq"],
        "events": buf["events"][max(0, after - (buf["seq"] - len(buf["events"]))):],
    }


def _tape_append(conv_id: int, event: dict):
    buf = _tape_buffers.setdefault(
        conv_id, {"seq": 0, "running": False, "events": []}
    )
    trimmed = {k: v for k in _TAPE_FIELDS if (v := event.get(k)) is not None}
    for k, v in trimmed.items():
        if isinstance(v, str) and len(v) > _TAPE_VALUE_CAP:
            trimmed[k] = v[:_TAPE_VALUE_CAP]
        elif isinstance(v, dict):
            # Structured fields (ask_user's args, #93) feed the question
            # card: cap their string values in place so the payload stays
            # usable (bounded, not dropped) instead of let loose in the
            # buffer.
            trimmed[k] = {
                ik: (iv[:_TAPE_VALUE_CAP] if isinstance(iv, str) and len(iv) > _TAPE_VALUE_CAP else iv)
                for ik, iv in v.items()
            }
        elif isinstance(v, list) and len(json.dumps(v, ensure_ascii=False)) > _TAPE_VALUE_CAP:
            trimmed[k] = {"truncated": True}
    buf["events"].append(trimmed)
    buf["seq"] += 1
    if len(buf["events"]) > TAPE_EVENT_CAP:
        del buf["events"][: len(buf["events"]) - TAPE_EVENT_CAP]


# ---- schedule math ----------------------------------------------------------

def parse_schedule_spec(schedule_spec: str) -> dict:
    try:
        spec = json.loads(schedule_spec or "{}")
        return spec if isinstance(spec, dict) else {}
    except json.JSONDecodeError:
        return {}


def normalize_schedule(schedule_type: str, schedule_spec) -> tuple[str, str]:
    """Validate a (type, spec) pair; returns the sanitized JSON spec string.
    Accepts a dict or a JSON string (rows come back from SQLite as text).
    Invalid pieces fall back to a 60-minute interval rather than poisoning
    the fire computation every tick."""
    if isinstance(schedule_spec, str):
        schedule_spec = parse_schedule_spec(schedule_spec)
    schedule_spec = schedule_spec or {}
    stype = schedule_type if schedule_type in VALID_SCHEDULE_TYPES else "interval"
    if stype == "interval":
        try:
            minutes = int(schedule_spec.get("minutes"))
        except (TypeError, ValueError):
            minutes = 60
        minutes = max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, minutes))
        return stype, json.dumps({"minutes": minutes})
    time_of_day = str(schedule_spec.get("time") or "09:00").strip()
    try:
        datetime.strptime(time_of_day, "%H:%M")
    except ValueError:
        time_of_day = "09:00"
    if stype == "weekly":
        try:
            weekday = int(schedule_spec.get("weekday"))
        except (TypeError, ValueError):
            weekday = 0
        weekday = max(0, min(6, weekday))  # 0 = Monday
        return stype, json.dumps({"weekday": weekday, "time": time_of_day})
    return stype, json.dumps({"time": time_of_day})


def compute_next_fire(
    schedule_type: str, schedule_spec: str, after: datetime | None = None
) -> datetime:
    """The next fire strictly AFTER `after` (local time) — the "never
    immediately on save" rule falls out of this directly: save-time passes
    `now`, so the earliest possible fire is one full interval / the next
    clock slot away."""
    after = after or datetime.now()
    stype, spec_str = normalize_schedule(schedule_type, schedule_spec)
    spec = parse_schedule_spec(spec_str)
    if stype == "interval":
        return after + timedelta(minutes=spec["minutes"])
    scheduled = datetime.strptime(spec["time"], "%H:%M")
    candidate = after.replace(hour=scheduled.hour, minute=scheduled.minute,
                              second=0, microsecond=0)
    if stype == "weekly":
        candidate += timedelta(days=(spec["weekday"] - after.weekday()) % 7)
    if candidate <= after:
        candidate += timedelta(days=7 if stype == "weekly" else 1)
    return candidate


def describe_schedule(schedule_type: str, schedule_spec: str) -> str:
    stype, spec_str = normalize_schedule(schedule_type, schedule_spec)
    spec = parse_schedule_spec(spec_str)
    if stype == "interval":
        minutes = spec["minutes"]
        if minutes % 60 == 0:
            n = minutes // 60
            return f"every {n} hour{'s' if n != 1 else ''}"
        return f"every {minutes} min"
    if stype == "daily":
        return f"daily at {spec['time']}"
    return f"weekly {WEEKDAY_NAMES[spec['weekday']]} at {spec['time']}"


# ---- global retry setting ---------------------------------------------------

def get_retry_settings(cfg: dict | None = None) -> tuple[int, int]:
    """(retry_count, backoff_minutes) from the global config "agents" block."""
    cfg = cfg if cfg is not None else load_config()
    block = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    try:
        count = max(0, min(10, int(block.get("retry_count", 2))))
    except (TypeError, ValueError):
        count = 2
    try:
        backoff = max(1, min(1440, int(block.get("retry_backoff_minutes", 5))))
    except (TypeError, ValueError):
        backoff = 5
    return count, backoff


def save_retry_settings(retry_count: int, retry_backoff_minutes: int):
    save_config({"agents": {
        "retry_count": max(0, min(10, int(retry_count))),
        "retry_backoff_minutes": max(1, min(1440, int(retry_backoff_minutes))),
    }})


# ---- firing -----------------------------------------------------------------

def _is_due(agent: dict, now: datetime) -> bool:
    if not agent.get("enabled") or not (agent.get("prompt") or "").strip():
        return False
    nxt = agent.get("next_fire_at")
    if not nxt:
        return False
    try:
        return datetime.fromisoformat(nxt) <= now
    except ValueError:
        return False


async def fire_agent(agent: dict, is_retry: bool = False) -> str:
    """Trigger one run of `agent` now. Returns 'started' | 'busy' | 'gone'.

    Schedule advance happens here, at fire time, from `now` — so "Run now"
    (API) and a due tick behave identically. A retry fire keeps the regular
    slot already parked in _retry_state instead of pushing it out again."""
    from backend.agent import loop as loop_mod

    aid = agent["id"]
    conv_id = agent.get("conversation_id") or 0
    conv = await get_conversation(conv_id) if conv_id else None
    if conv is None:
        log.warning("agent %s: conversation is gone; disabling", aid)
        await _patch(aid, enabled=False, last_status="error",
                           last_finished_at=datetime.now().isoformat(timespec="seconds"))
        _retry_state.pop(aid, None)
        return "gone"
    if not agent.get("enabled"):
        # Defense in depth: the tick already skips disabled agents, but a
        # run-now on a paused agent must not start (or resurrect a past slot)
        # either. Any stale next_fire_at stays stale until the agent resumes.
        return "disabled"
    if loop_mod.agent_is_running(conv_id):
        # Postpone instead of losing the run to the per-conversation lock.
        await _patch(
            aid, next_fire_at=(datetime.now() + timedelta(seconds=BUSY_RETRY_SECONDS))
            .isoformat(timespec="seconds")
        )
        return "busy"

    now = datetime.now()
    state = _retry_state.get(aid)
    if not is_retry:
        # Regular fire: park the schedule's next slot; retries reuse it.
        state = {"attempts": 0,
                 "scheduled_next": compute_next_fire(
                     agent["schedule_type"], agent["schedule_spec"], now
                 ).isoformat(timespec="seconds")}
        _retry_state[aid] = state
    await _patch(
        aid,
        last_fired_at=now.isoformat(timespec="seconds"),
        last_status="running",
        next_fire_at=state["scheduled_next"] if state else compute_next_fire(
            agent["schedule_type"], agent["schedule_spec"], now
        ).isoformat(timespec="seconds"),
    )

    # Effective prompt (issue #41): the user's prompt verbatim + standing
    # instructions. No auto-prepended context, no template variables.
    instructions = await list_instructions(aid)
    prompt = agent["prompt"]
    if instructions:
        lines = "\n".join(f"- {i['content']}" for i in instructions)
        prompt = f"{prompt}\n\n# Standing instructions\n\n{lines}"

    task = asyncio.create_task(
        _run_and_settle(
            aid,
            conv_id,
            prompt,
            conv.get("workspace") or "",
            agent["approval_policy"],
            bool(agent["memory_enabled"]),
            agent.get("model") or "",
            agent.get("effort") or "",
            agent.get("retention") or 0,
            bool(agent.get("allow_ask_user")),
        )
    )
    _fire_tasks.add(task)
    task.add_done_callback(_fire_tasks.discard)
    return "started"


async def _run_and_settle(
    aid: str,
    conv_id: int,
    prompt: str,
    workspace: str,
    policy: str,
    memory_enabled: bool,
    model: str,
    effort: str,
    retention: int,
    allow_ask_user: bool = False,
):
    """Consume one fire's run to completion, then settle the outcome:
    status recording (the toast source), global retry scheduling, and the
    per-agent retention trim."""
    from backend.agent.loop import run_agent

    ok, error_text = True, ""
    _tape_buffers[conv_id] = {"seq": 0, "running": True, "events": []}
    try:
        async for line in run_agent(
            conv_id,
            prompt,
            workspace,
            policy=policy,
            include_history=memory_enabled,
            model_override=model,
            effort_override=effort,
            allow_ask_user=allow_ask_user,
        ):
            # The loop persists the transcript itself; here we only relay
            # the events into the per-conversation tape buffer so the open
            # chat can poll them for its live telemetry. An in-band error
            # event (e.g. provider misconfigured) counts as a failed fire
            # just like a raised exception.
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                _tape_append(conv_id, event)
            if event.get("type") == "error":
                ok, error_text = False, str(event.get("message", "run failed"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a failed fire must not kill the scheduler
        ok, error_text = False, str(exc)
    finally:
        if conv_id in _tape_buffers:
            _tape_buffers[conv_id]["running"] = False

    if not ok:
        log.warning("scheduled agent %s fire failed: %s", aid, error_text)
        state = _retry_state.get(aid)
        attempts = (state or {}).get("attempts", 0) + 1
        retry_count, backoff = get_retry_settings()
        if state is not None and attempts <= retry_count:
            state["attempts"] = attempts
            # The retry re-enters through the due-tick at now+backoff; the
            # regular schedule slot stays parked in _retry_state.
            retry_at = (datetime.now() + timedelta(minutes=backoff)).isoformat(
                timespec="seconds"
            )
            await _patch(
                aid,
                next_fire_at=retry_at,
                last_finished_at=datetime.now().isoformat(timespec="seconds"),
                last_status="error",
            )
            return
    # Success, or retries exhausted: the parked slot becomes the schedule.
    _retry_state.pop(aid, None)
    await _ensure_future_slot(aid)
    now_iso = datetime.now().isoformat(timespec="seconds")
    await _patch(aid, last_finished_at=now_iso, last_status="ok" if ok else "error")
    if retention > 0:
        with contextlib.suppress(Exception):
            await trim_agent_transcript(conv_id, retention)


async def _ensure_future_slot(aid: str):
    """Roll a run's slot forward if it lands in the past at settle time.

    While a run is in flight, every due tick busy-postpones next_fire_at to
    ~60s ahead; a run that outlives its schedule (or a stop) leaves that
    slot in the past, and without this roll the next tick would immediately
    re-fire the run the user just stopped. Missed slots collapse into the
    single next future slot — there is deliberately no backlog."""
    row = await get_agent(aid)
    if row is None:
        return
    nxt = row.get("next_fire_at")
    try:
        overdue = not nxt or datetime.fromisoformat(nxt) <= datetime.now()
    except (TypeError, ValueError):
        overdue = True
    if overdue:
        await _patch(
            aid,
            next_fire_at=compute_next_fire(
                row.get("schedule_type") or "interval",
                row.get("schedule_spec") or "{}",
                datetime.now(),
            ).isoformat(timespec="seconds"),
        )


def cancel_agent_run(conversation_id: int):
    """Ask the in-flight run in this conversation to stop after its current
    step. Scheduler-side wrapper so callers (API layer) don't reach into the
    loop module directly."""
    from backend.agent import loop as loop_mod

    loop_mod.cancel_agent(conversation_id)


def clear_retry_state(agent_id: str):
    """Drop pending retry bookkeeping for an agent — used when it is paused
    or deleted, so a scheduled retry can't resurrect it."""
    _retry_state.pop(agent_id, None)


async def _tick():
    """Fire every due enabled agent — each as its own task, so agents run
    in parallel with each other and with the user's active turn."""
    now = datetime.now()
    for agent in await list_agents():
        if not _is_due(agent, now):
            continue
        try:
            await fire_agent(agent, is_retry=agent["id"] in _retry_state)
        except Exception:  # noqa: BLE001 — keep the tick alive
            log.exception("failed to fire agent %s", agent.get("id"))


async def _loop():
    try:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            await _tick()
    except asyncio.CancelledError:
        raise


async def startup_roll_forward():
    """Skip-missed-fires semantics (issue #41): anything that came due while
    no backend was alive is skipped SILENTLY — its next_fire_at rolls
    forward to the next future slot. No catch-up fire."""
    now = datetime.now()
    for agent in await list_agents():
        # A restart orphans nothing (runs live in this process), so a row
        # still marked "running" is a crash/interrupt leftover — record it
        # as such instead of leaving the UI showing a run that never ended.
        if agent.get("last_status") == "running":
            await _patch(agent["id"], last_status="error",
                         last_finished_at=now.isoformat(timespec="seconds"))
        nxt = agent.get("next_fire_at")
        if not nxt:
            await _patch(
                agent["id"],
                next_fire_at=compute_next_fire(
                    agent["schedule_type"], agent["schedule_spec"], now
                ).isoformat(timespec="seconds"),
            )
            continue
        try:
            overdue = datetime.fromisoformat(nxt) <= now
        except ValueError:
            overdue = True
        if overdue:
            await _patch(
                agent["id"],
                next_fire_at=compute_next_fire(
                    agent["schedule_type"], agent["schedule_spec"], now
                ).isoformat(timespec="seconds"),
            )


def start_scheduler() -> bool:
    """Start the background tick if any agent is configured. Returns whether
    a scheduler is now running (also True when one was already active)."""
    global _task
    if _task is not None and not _task.done():
        return True
    # The DB is async; the mere presence check happens in the caller's loop
    # via ensure_scheduled — here we only guard double-start and the
    # config-only case where the API layer knows no agents exist yet.
    _task = asyncio.create_task(_loop())
    return True


async def ensure_scheduled():
    """Called after agent CRUD and at boot: roll overdue slots forward
    (skip-missed) and make sure the tick task is running when at least one
    agent exists. The tick re-reads the DB every pass, so new/edited agents
    are picked up without a restart."""
    if await start_if_needed():
        await startup_roll_forward()


async def start_if_needed() -> bool:
    global _task
    if _task is not None and not _task.done():
        return True
    if not await list_agents():
        return False
    _task = asyncio.create_task(_loop())
    return True


def stop_scheduler():
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            pass
    _task = None
    _retry_state.clear()
