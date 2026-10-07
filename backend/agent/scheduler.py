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
import asyncio
import json
import logging
import re
import subprocess
from datetime import datetime, timedelta

from backend.agent import gitexec
from backend.agent.config import load_config, qualify_model_scope, save_config
from backend.db.database import (
    get_agent,
    get_conversation,
    list_agents,
    list_instructions,
    trim_agent_transcript,
    update_agent as _db_update_agent,
    update_conversation,
)


def _patch(agent_id: str, **fields) -> dict | None:
    """Merge-update one agent row (dict-form wrapper over the DB layer)."""
    return _db_update_agent(agent_id, fields)

log = logging.getLogger("yaah.scheduler")

VALID_SCHEDULE_TYPES = ("interval", "daily", "weekly")
VALID_POLICIES = ("sandbox-only", "autonomous")
# #278: where a scheduled fire's work lands. 'off' (default) = today's
# behavior — the pinned chat runs on its own selected branch; 'fixed' =
# every fire lands on landing_branch; 'per-run' = each fire gets its own
# branch, left unmerged for manual integration.
VALID_LANDING_MODES = ("off", "fixed", "per-run")
# #296: when a fired run's spoken briefing gets spoken — arrival (the
# default: as the fire emits it, chat on screen or not) or visible (held
# until the conversation becomes the on-screen one). Consumed by the
# frontend's say watcher; the backend only stores and validates it.
VALID_SAY_MODES = ("arrival", "visible")
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


async def resolve_landing(agent_id: str) -> tuple[str, str]:
    """#278: the (mode, branch) landing setting of `agent_id`, normalized.

    Unknown/blank modes read as 'off'. landing_branch is honored only in
    fixed mode, and only when it exists in the workspace's repo at fire
    time — a dead target would hand the run a stale pin (#302), so the
    fire keeps the chat's own branch instead and the miss is logged.

    #337: remote workspaces resolve through the #333 gateway exactly as
    local — the branch list comes from the host repo, and the write-through
    to the chat pin proceeds identically. The one degradation is host
    unreachable: the fire cannot ask the host anything, so it keeps the
    chat pin and logs (a distinguishable arm from a live host's
    missing-branch miss)."""
    agent = await get_agent(agent_id)
    if not agent:
        return "off", ""
    mode = str(agent.get("landing_mode") or "off").strip() or "off"
    if mode not in VALID_LANDING_MODES:
        mode = "off"
    if mode != "fixed":
        return mode, ""
    branch = str(agent.get("landing_branch") or "").strip()
    if not branch:
        return "off", ""
    from backend.agent.gitinfo import is_git_repo, list_local_branches
    from backend.agent.tools import workspace_root

    workspace = agent.get("workspace") or ""
    remote = gitexec.parse_ns(workspace) is not None
    if remote:
        # The NAMESPACED string is the routing unit — resolving it to a
        # local-looking path would silently misroute the gateway.
        target = workspace
    else:
        target = workspace_root(workspace)
        if not is_git_repo(target):
            log.warning(
                "agent %s: fixed landing branch %r not found locally; fire keeps the chat pin",
                agent_id, branch,
            )
            return "off", ""
    branches = await list_local_branches(target)
    if branch not in branches:
        if remote and not branches:
            # An absent answer is not an empty repo: ask once whether the
            # host could not be reached or the workspace is not a repo,
            # so the log names the real reason.
            probe = await gitexec.run_git(target, "rev-parse", "--git-dir")
            why = (
                "workspace is not a git repository"
                if probe is not None and probe[0] != 0
                else "host unreachable"
            )
            log.warning(
                "agent %s: %s, cannot check fixed landing branch %r; "
                "fire keeps the chat pin",
                agent_id, why, branch,
            )
        else:
            log.warning(
                "agent %s: fixed landing branch %r not found; fire keeps the chat pin",
                agent_id, branch,
            )
        return "off", ""
    return "fixed", branch


async def _create_per_run_branch(
    workspace: str, agent_id: str, start_point: str | None = None
) -> str | None:
    """#278: create this fire's per-run branch and return its name, or
    None when creation is impossible. Deterministic name
    `<agent>-<YYYYMMDD-HHMM>` (slug of the agent id), collision-bumped
    with -2, -3… — a branch is never overwritten, and two fires in the
    same minute each get their own. Start point is the chat's selected
    branch when it exists (ADR-0010: new branches derive from the chat's
    selection, never the primary's HEAD), else HEAD.

    #337: remote workspaces create the branch ON THE HOST through the
    #333 gateway — same naming, same bump rule, same refusal to
    overwrite. Host unreachable (or any unrunnable command) returns
    None: creation failure keeps the chat's current pin."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(agent_id).lower()).strip("-") or "agent"
    base = f"{slug}-{datetime.now().strftime('%Y%m%d-%H%M')}"

    if gitexec.parse_ns(workspace) is None:
        # ------------------------------------------------- local arm: as before
        from backend.agent.gitinfo import is_git_repo
        from backend.agent.tools import workspace_root

        try:
            root = workspace_root(workspace)
        except ValueError:
            return None
        if not is_git_repo(root):
            return None

        def _fresh_branches():
            # Raw git, not list_local_branches: the helper is TTL-cached,
            # and a bump must see the branch the previous attempt created.
            proc = subprocess.run(
                ["git", "branch", "--format=%(refname:short)"],
                cwd=str(root), capture_output=True, text=True, timeout=30,
            )
            return set(proc.stdout.split()) if proc.returncode == 0 else set()

        existing = _fresh_branches()
        start = start_point if start_point in existing else None
        name, n = base, 2
        for _ in range(8):
            args = ["git", "branch", name] + ([start] if start else [])
            proc = await asyncio.to_thread(
                subprocess.run, args, cwd=str(root), capture_output=True, text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                return name
            existing = _fresh_branches()
            if name in existing:
                # Collision (another fire won the race): bump and retry — a
                # branch is never overwritten.
                name = f"{base}-{n}"
                n += 1
                continue
            log.warning(
                "agent %s: per-run branch %s creation failed: %s",
                agent_id, name, (proc.stderr or "").strip(),
            )
            return None
        log.warning("agent %s: per-run branch naming exhausted after 8 bumps", agent_id)
        return None

    # ------------------------------------------------------- remote arm (#337)
    # The NAMESPACED workspace is the routing unit — workspace_root would
    # resolve it into a local-looking path the gateway never sees.
    # One cheap hop decides reachability AND repo-ness before any branch
    # command ships; a not-a-repo host dir never sees one.
    probe = await gitexec.run_git(workspace, "rev-parse", "--git-dir")
    if probe is None or probe[0] != 0:
        why = (
            "workspace is not a git repository"
            if probe is not None
            else "host unreachable"
        )
        log.warning(
            "agent %s: per-run branch creation failed: %s", agent_id, why,
        )
        return None

    # Raw gateway listing, not a cached helper: a bump must see the branch
    # the previous attempt just created. Plain `git branch`, not
    # --format (parens/percent are forbidden by the cross-dialect
    # corpus) — parsed exactly as #335's remote branch list is.
    # None = the gateway could not run git (host offline/channel).
    async def _host_branches() -> set[str] | None:
        from backend.agent.gitinfo import _plain_branch_names

        res = await gitexec.run_git(workspace, "branch")
        if res is None:
            return None
        return set(_plain_branch_names(res[1] or "")) if res[0] == 0 else set()

    async def _host_create(name: str, start: str | None) -> tuple[int, str] | None:
        return await gitexec.run_git(
            workspace, "branch", name, *([start] if start else [])
        )

    existing = await _host_branches()
    start = start_point if start_point in (existing or set()) else None
    name, n = base, 2
    for _ in range(8):
        res = await _host_create(name, start)
        if res is not None and res[0] == 0:
            return name
        existing = await _host_branches()
        if existing is not None and name in existing:
            # Collision (another fire won the race): bump and retry — a
            # branch is never overwritten.
            name = f"{base}-{n}"
            n += 1
            continue
        if existing is None:
            log.warning(
                "agent %s: per-run branch creation failed: host unreachable",
                agent_id,
            )
        else:
            log.warning(
                "agent %s: per-run branch %s creation failed: %s",
                agent_id, name,
                ((res[1] if res else "") or "git refused").strip(),
            )
        return None
    log.warning("agent %s: per-run branch naming exhausted after 8 bumps", agent_id)
    return None


async def fire_agent(
    agent: dict, is_retry: bool = False, one_shot: bool = False
) -> str:
    """Trigger one run of `agent` now. Returns 'started' | 'busy' | 'gone'.

    Schedule advance happens here, at fire time, from `now` — so "Run now"
    (API) and a due tick behave identically. A retry fire keeps the regular
    slot already parked in _retry_state instead of pushing it out again.

    one_shot (#199): a deliberate single run that ignores the enabled gate —
    a paused agent fires once and STAYS paused. Its parked next_fire_at is
    left untouched (no resurrection, no roll-forward, no retry slot); only
    the resume path (enable PATCH) rolls a stale slot. The busy postpone
    still applies: the per-conversation lock is load-bearing either way."""
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
    if not agent.get("enabled") and not one_shot:
        # Defense in depth: the tick already skips disabled agents, but a
        # run-now on a paused agent must not start (or resurrect a past slot)
        # either. Any stale next_fire_at stays stale until the agent resumes.
        return "disabled"
    parked = one_shot
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
    fire_fields = {
        "last_fired_at": now.isoformat(timespec="seconds"),
        "last_status": "running",
    }
    if not parked:
        # A one-shot never advances (or resurrects) the schedule slot.
        fire_fields["next_fire_at"] = (
            state["scheduled_next"] if state else compute_next_fire(
                agent["schedule_type"], agent["schedule_spec"], now
            ).isoformat(timespec="seconds")
        )
    await _patch(aid, **fire_fields)

    # #278: resolve this fire's landing (where its work should end up) and
    # write it through to the pinned chat before the run starts. The run
    # path already reads conv["selected_branch"] to materialize the chat
    # worktree and inject the run SOP, so the write-through reuses the
    # whole #277 machinery and the landing stays prompt-driven. Deferred
    # imports: the scheduler must import without touching the git layer.
    # #337: resolution routes local vs remote through the git gateway, so
    # remote fires steer identically (fixed validates against the host's
    # branches; per-run creates on the host). Keep the primary worktree
    # out of it entirely — the harness never moves a branch checked out
    # in the primary (ADR-0010).
    landing_mode, landing_branch = await resolve_landing(aid)
    if landing_mode == "fixed":
        await update_conversation(
            conv_id, selected_branch=landing_branch, branch_pin_origin="explicit"
        )
    elif landing_mode == "per-run":
        created = await _create_per_run_branch(conv.get("workspace") or "", aid)
        if created is None:
            log.warning(
                "agent %s: per-run branch creation failed; the run keeps the "
                "chat's current pin", aid,
            )
        else:
            await update_conversation(
                conv_id, selected_branch=created, branch_pin_origin="explicit"
            )

    # Effective prompt (issue #41): the user's prompt verbatim + standing
    # instructions. No auto-prepended context, no template variables.
    instructions = await list_instructions(aid)
    prompt = agent["prompt"]
    if instructions:
        lines = "\n".join(f"- {i['content']}" for i in instructions)
        prompt = f"{prompt}\n\n# Standing instructions\n\n{lines}"
    if landing_mode == "per-run":
        # #278, per-run mode: each fire's work lands on its own branch,
        # which stays unmerged for manual integration (ADR-0010: the
        # harness never merges into shared trees unattended). Landing
        # still happens inside the chat worktree per the selector note;
        # this line stops the agent from deleting the branch after it.
        prompt += (
            "\n\n# Landing\n\nThis is a scheduled per-run fire: your work "
            "lands on the branch named in the branch-selector note above. "
            "Leave that branch in place for manual integration — never "
            "delete it and never merge it into a shared branch."
        )

    task = asyncio.create_task(
        _run_and_settle(
            aid,
            conv_id,
            prompt,
            conv.get("workspace") or "",
            agent["approval_policy"],
            bool(agent["memory_enabled"]),
            # #132: qualify a legacy bare id at the fire site too — the
            # repair pass normally handles old rows at boot, but a boot
            # where providers were unreachable retries NEXT boot; a fire
            # in that window must not drift to ambient active_provider.
            qualify_model_scope(agent.get("model") or ""),
            agent.get("effort") or "",
            agent.get("retention") or 0,
            bool(agent.get("allow_ask_user")),
            one_shot=one_shot,
        )
    )
    _fire_tasks.add(task)
    task.add_done_callback(_fire_tasks.discard)
    return "started"


def _is_rate_limit(error_text: str) -> bool:
    """True when a fire failure is a provider rate-limit / quota error.

    Matched loosely on the message text — the error arrives either as a
    ModelError string ("Model API error 429: ...") or as an in-band error
    event, and providers word their quota messages differently."""
    t = (error_text or "").lower()
    return (
        "429" in t
        or "rate limit" in t
        or "rate-limit" in t
        or "limit exhausted" in t
        or "quota" in t
        or "too many requests" in t
    )


def _settle_source(ok: bool, error_text: str) -> str:
    """#243 AC 1: name WHY a fire settles the way it does, for the log —
    the reported false toast left no visible trace, so the settle source
    must become observable."""
    if ok:
        return "run completed"
    if error_text:
        return f"in-band error event: {error_text[:200]}" if not _is_rate_limit(
            error_text) else f"rate limit: {error_text[:200]}"
    return "exception with empty message"


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
    one_shot: bool = False,
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
            # #198: tag the persisted effective-prompt row so the UI can
            # collapse it to a chip; content stays the model input verbatim.
            user_meta={"agent_prompt": True},
        ):
            # The loop persists the transcript itself; here we only relay
            # the events into the per-conversation tape buffer so the open
            # chat can poll them for its live telemetry. An in-band FATAL
            # error event (e.g. provider failure that ended the turn) counts
            # as a failed fire just like a raised exception; non-fatal ones
            # (claim refusal / busy conversation, #243) end the stream
            # without the run having failed — settled 'ok', logged below.
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                _tape_append(conv_id, event)
            if event.get("type") == "error" and event.get("fatal", True):
                ok, error_text = False, str(event.get("message", "run failed"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a failed fire must not kill the scheduler
        ok, error_text = False, str(exc)
    finally:
        if conv_id in _tape_buffers:
            _tape_buffers[conv_id]["running"] = False

    if one_shot:
        # #199: a one-shot's outcome never touches the schedule — no retry
        # slot, no roll-forward — the paused agent's parked next_fire_at
        # stays exactly as it was. Only the run bookkeeping settles.
        _retry_state.pop(aid, None)
        status = "ok" if ok else ("error_quiet" if _is_rate_limit(error_text) else "error")
        log.info(
            "agent %s one-shot settled %s (source: %s)", aid, status,
            _settle_source(ok, error_text),
        )
        if not ok:
            log.warning("scheduled agent %s one-shot failed: %s", aid, error_text)
        await _patch(
            aid,
            last_finished_at=datetime.now().isoformat(timespec="seconds"),
            last_status=status,
        )
        if retention > 0:
            with contextlib.suppress(Exception):
                await trim_agent_transcript(conv_id, retention)
        return

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
            # Rate-limit fires (429 / quota exhausted) are transient provider
            # outages, not agent failures: record them as 'error_quiet' — the
            # toast watcher only fires on exact 'error' — so one outage
            # doesn't spam the user with a toast per retry fire. The final
            # failure after retries are exhausted still settles 'error'.
            status = "error_quiet" if _is_rate_limit(error_text) else "error"
            await _patch(
                aid,
                next_fire_at=retry_at,
                last_finished_at=datetime.now().isoformat(timespec="seconds"),
                last_status=status,
            )
            return
    # Success, or retries exhausted: the parked slot becomes the schedule.
    _retry_state.pop(aid, None)
    await _ensure_future_slot(aid)
    now_iso = datetime.now().isoformat(timespec="seconds")
    status = "ok" if ok else "error"
    log.info(
        "agent %s settled %s (source: %s)", aid, status,
        _settle_source(ok, error_text),
    )
    await _patch(aid, last_finished_at=now_iso, last_status=status)
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
