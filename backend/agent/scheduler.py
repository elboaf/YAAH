"""Scheduled agents (issue #41): user-defined recurring agent runs.

Each agent lives in config.json under ``agents``: a list of
{id, conversation_id, name, prompt, kind, interval_minutes, time, weekday,
policy, enabled, last_run_at, next_run_at}. Every agent owns a pinned
conversation (created with the agent); when its schedule comes due — and a
backend process is alive, desktop or headless service — the scheduler fires
one turn of ``prompt`` into that conversation.

Per-agent approval policy: "sandbox-only" (default) never blocks on the
access-mode gate — approval-required tools are skipped with an explanatory
note and the run continues; "ask" behaves like a normal chat turn; "full"
runs ungated regardless of the global access mode.
"""
import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timedelta

from backend.agent.config import load_config, save_config

log = logging.getLogger("yaah.scheduler")

VALID_KINDS = ("interval", "daily", "weekly")
VALID_POLICIES = ("sandbox-only", "ask", "full")

# Schedule sanity bounds: an interval below this would hammer the model
# provider; above a month makes "next run" effectively meaningless.
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 60 * 24 * 30

# How often the scheduler wakes up to look for due agents. A 30s tick means
# a fire is at most half a minute late, which is plenty for human schedules.
TICK_SECONDS = 30

# When an agent's conversation is mid-turn at fire time, retry after this
# long instead of advancing the schedule (no run is silently dropped).
BUSY_RETRY_SECONDS = 60

_task: asyncio.Task | None = None


# ---- agents config helpers -------------------------------------------------

def get_agents(cfg: dict | None = None) -> list[dict]:
    cfg = cfg if cfg is not None else load_config()
    agents = cfg.get("agents")
    return [a for a in agents if isinstance(a, dict)] if isinstance(agents, list) else []


def _save_agents(agents: list[dict]):
    save_config({"agents": agents})


def sanitize_agent(raw: dict) -> dict:
    """Coerce one agent dict into a valid, fully-populated record. Unknown
    fields are dropped so the config file stays clean."""
    try:
        conv_id = int(raw.get("conversation_id") or 0)
    except (TypeError, ValueError):
        conv_id = 0
    name = str(raw.get("name") or "").strip() or "Agent"
    kind = raw.get("kind") if raw.get("kind") in VALID_KINDS else "interval"
    interval = 60
    raw_interval = raw.get("interval_minutes")
    if raw_interval not in (None, ""):
        try:
            interval = int(raw_interval)
        except (TypeError, ValueError):
            pass
    interval = max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, interval))
    time_of_day = str(raw.get("time") or "09:00").strip()
    # A malformed HH:MM falls back to the default rather than poisoning
    # compute_next_run every tick.
    try:
        datetime.strptime(time_of_day, "%H:%M")
    except ValueError:
        time_of_day = "09:00"
    try:
        weekday = int(raw.get("weekday") or 0)
    except (TypeError, ValueError):
        weekday = 0
    weekday = max(0, min(6, weekday))  # 0 = Monday
    policy = raw.get("policy") if raw.get("policy") in VALID_POLICIES else "sandbox-only"
    aid = str(raw.get("id") or "").strip() or uuid.uuid4().hex[:12]
    return {
        "id": aid,
        "conversation_id": conv_id,
        "name": name,
        "prompt": str(raw.get("prompt") or "").strip(),
        "kind": kind,
        "interval_minutes": interval,
        "time": time_of_day,
        "weekday": weekday,
        "policy": policy,
        "enabled": bool(raw.get("enabled", True)),
        "last_run_at": str(raw.get("last_run_at") or ""),
        "next_run_at": str(raw.get("next_run_at") or ""),
    }


def compute_next_run(agent: dict, now: datetime | None = None) -> datetime:
    """The agent's next fire time, local time. Interval kinds count from
    `now` (schedule drift after sleeps/app-closures is fine for this use);
    daily/weekly land on the next occurrence of the configured wall time."""
    now = now or datetime.now()
    if agent["kind"] == "interval":
        return now + timedelta(minutes=agent["interval_minutes"])
    scheduled = datetime.strptime(agent["time"], "%H:%M")
    candidate = now.replace(hour=scheduled.hour, minute=scheduled.minute,
                            second=0, microsecond=0)
    if agent["kind"] == "weekly":
        days_ahead = (agent["weekday"] - now.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        # Same weekday but the time already passed: roll a full week.
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate
    # daily
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def upsert_agent(raw: dict) -> dict:
    """Validate + insert (or update by id) one agent, recomputing
    next_run_at. Returns the sanitized record as saved."""
    agents = get_agents()
    record = sanitize_agent(raw)
    existing = None
    if raw.get("id"):
        existing = next((a for a in agents if a.get("id") == record["id"]), None)
    # A schedule edit restarts the clock from now; an explicit next_run_at
    # (tests, internal re-bookkeeping like the busy-postpone) wins; a record
    # that never ran computes its first slot.
    if raw.get("next_run_at"):
        record["next_run_at"] = str(raw["next_run_at"])
    elif existing and not any(k in raw for k in ("kind", "interval_minutes", "time", "weekday")):
        record["next_run_at"] = existing.get("next_run_at") or _iso(
            compute_next_run(record)
        )
    else:
        record["next_run_at"] = _iso(compute_next_run(record))
    if existing:
        agents[agents.index(existing)] = record
    else:
        agents.append(record)
    _save_agents(agents)
    return record


def remove_agent(agent_id: str) -> bool:
    agents = get_agents()
    remaining = [a for a in agents if a.get("id") != agent_id]
    if len(remaining) == len(agents):
        return False
    _save_agents(remaining)
    return True


def _update_agent(agent_id: str, **fields):
    agents = get_agents()
    for i, a in enumerate(agents):
        if a.get("id") == agent_id:
            agents[i] = {**a, **fields}
            _save_agents(agents)
            return agents[i]
    return None


def agent_conversation_ids() -> set[int]:
    return {a["conversation_id"] for a in get_agents() if a.get("conversation_id")}


# ---- firing ----------------------------------------------------------------

async def _drain_run(conversation_id: int, prompt: str, workspace: str, policy: str):
    """Fire one scheduled turn: run the loop to completion, discarding the
    event stream (the loop persists both sides of the conversation itself,
    so the transcript simply appears in the pinned chat)."""
    from backend.agent.loop import run_agent

    try:
        async for _line in run_agent(
            conversation_id, prompt, workspace, policy=policy
        ):
            pass
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a failed scheduled run must not kill the scheduler
        log.exception("scheduled agent run failed (conversation %s)", conversation_id)


async def fire_agent(agent: dict, advance_schedule: bool = True) -> bool:
    """Trigger one run of `agent` now. Returns True when a run actually
    started; False when the conversation was busy (schedule postponed) or
    the conversation no longer exists."""
    from backend.agent import loop as loop_mod
    from backend.agent.config import CONFIG_PATH  # noqa: F401 — import cost only
    from backend.db.database import get_conversation

    conv_id = agent.get("conversation_id") or 0
    conv = await get_conversation(conv_id) if conv_id else None
    if conv is None:
        log.warning("agent %s: conversation %s is gone; disabling", agent["id"], conv_id)
        _update_agent(agent["id"], enabled=False)
        return False
    if loop_mod.agent_is_running(conv_id):
        # Busy: retry soon rather than dropping the run. Not advancing
        # last_run_at/next_run_at keeps the schedule anchored.
        _update_agent(agent["id"], next_run_at=_iso(
            datetime.now() + timedelta(seconds=BUSY_RETRY_SECONDS)
        ))
        return False

    now = datetime.now()
    updates: dict = {"last_run_at": _iso(now)}
    if advance_schedule:
        updates["next_run_at"] = _iso(compute_next_run(agent, now))
    _update_agent(agent["id"], **updates)

    prompt = agent["prompt"] or f"Run your scheduled task: {agent['name']}"
    # The scheduler-provided marker tells the model who is asking and why
    # there is no human watching (matters for ask_user-style behavior).
    prompt = (
        f"[Scheduled run of agent “{agent['name']}” — no user is watching "
        f"this chat right now.]\n\n{prompt}"
    )
    asyncio.create_task(
        _drain_run(conv_id, prompt, conv.get("workspace") or "", agent["policy"])
    )
    return True


# ---- scheduler loop ---------------------------------------------------------

def _due(agent: dict, now: datetime) -> bool:
    if not agent.get("enabled") or not agent.get("prompt"):
        return False
    nxt = _parse_iso(agent.get("next_run_at") or "")
    return nxt is not None and nxt <= now


async def _tick(catchup: bool = False):
    """Fire every due enabled agent. Runs sequentially: two agents coming
    due on the same tick start a few seconds apart instead of racing the
    provider, and each fire is guarded so one failure can't skip the rest."""
    now = datetime.now()
    for agent in get_agents():
        if not _due(agent, now):
            continue
        if agent.get("policy") not in VALID_POLICIES:
            continue
        try:
            await fire_agent(agent, advance_schedule=not catchup)
        except Exception:  # noqa: BLE001 — keep the tick alive
            log.exception("failed to fire agent %s", agent.get("id"))


async def _loop():
    try:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            await _tick()
    except asyncio.CancelledError:
        raise


async def startup_catchup():
    """Fire agents whose next_run_at passed while no backend was running
    (desktop app closed). One run each — no backlog replay."""
    now = datetime.now()
    stale = [
        a for a in get_agents()
        if a.get("enabled") and a.get("prompt")
        and (nxt := _parse_iso(a.get("next_run_at") or "")) is not None
        and nxt <= now
    ]
    for agent in stale:
        try:
            await fire_agent(agent)
        except Exception:  # noqa: BLE001
            log.exception("catch-up fire failed for agent %s", agent.get("id"))


def start_scheduler() -> bool:
    """Start the background tick if any agent is configured. Returns whether
    a scheduler is now running (also True when one was already active)."""
    global _task
    if _task is not None and not _task.done():
        return True
    if not any(a.get("enabled") for a in get_agents()):
        return False
    _task = asyncio.create_task(_loop())
    return True


def ensure_scheduled():
    """Re-check after agent CRUD: start the tick when the first agent
    appears; no-op while agents exist (the tick reads config fresh each
    pass, so new/edited agents are picked up without a restart)."""
    start_scheduler()


def stop_scheduler():
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            pass
    _task = None
