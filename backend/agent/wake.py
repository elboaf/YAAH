"""Wake bridge (issue #688): an external event file starts chat turns.

Some agent workflows produce events nobody forwards into a YAAH chat —
today's case: an impeccable live session. Its detached poll loop appends
every bar event (pick, steer, accept) to a journal on disk, but a journal
is just a file: only a running chat turn ever acts on it, and once the
driving chat's run ends, events pile up unhandled until a human pokes the
conversation.

This module closes that loop in-process, the same way the scheduler
(issue #41) closes the time loop: an asyncio task in the FastAPI lifespan
tails a configured file, and actionable events fire a turn of the pinned
conversation via run_agent. The chat then reads the journal and answers
the events itself — the same CLI commands a live session's driving agent
already uses.

Design decisions (mirroring the scheduler's settled postures):

- Config-driven ("wake" block), enabled=False by default: zero behavior
  change for every existing install. Partial merge at the Settings API,
  like voice/remote/computer_use.
- No catch-up: the watcher starts at the file's CURRENT end (boot forgets
  history), and anything older than the debounce window collapses into
  one wake naming the count — the same skip-missed spirit as
  startup_roll_forward.
- Busy target = queue, not a second stream: if the conversation is
  mid-turn the prompt goes through the existing queue (#7) and drains at
  the next step boundary. The per-conversation run lock stays the one
  guard; the watcher never fights it.
- Signal vs noise: only event types someone must answer fire a wake
  (heartbeat "timeout" lines never do). Config "types" overrides the
  default set.
- A crash or bad config disables the bridge, never the app: every poll
  is defended, fire failures are logged, the target going missing logs
  and skips without touching the config.

v1 is config-only (no Settings UI, single source file, single target
chat) — follow-ups tracked in the issue.
"""
import asyncio
import contextlib
import json
import logging
import os
import time

from backend.agent.config import load_config
from backend.db.database import get_conversation

log = logging.getLogger("yaah.wake")

# How often the watcher looks at the file. Cheap stat + (usually) a
# short read; well under any debounce a human-perceived flow needs.
TICK_SECONDS = 1.0

# Bursts of events (a pick + its generate, several steers) collapse into
# ONE wake this many seconds after the last actionable line.
DEFAULT_DEBOUNCE_SECONDS = 3.0

# The journal's heartbeat lines would wake the chat every few seconds
# forever — they are the poll loop saying "still alive", not work.
# The spec default is everything EXCEPT these: unknown event types wake
# the chat too, so a journal the chat doesn't recognize still gets read
# and answered by the one party that can interpret it.
DEFAULT_NOISE = frozenset({"timeout"})

DEBOUNCE_MIN, DEBOUNCE_MAX = 0.0, 60.0

_watcher: "WakeFileWatcher | None" = None
_task: asyncio.Task | None = None

# Strong refs to wake-fired run tasks (asyncio holds only weak refs to
# tasks — unreferenced ones can be GC'd mid-run and vanish silently).
_run_tasks: set[asyncio.Task] = set()


def read_wake_config(cfg: dict | None = None) -> dict:
    """The sanitized "wake" block. Garbage fields sanitize to their safe
    defaults rather than poisoning the watcher every boot (same posture
    as normalize_schedule); types must be a non-empty list of non-empty
    strings to count as an override. Accepts the spec's `file` key as an
    alias of `path` (the issue text and early docs say `file`)."""
    cfg = cfg if cfg is not None else load_config()
    block = cfg.get("wake") if isinstance(cfg.get("wake"), dict) else {}
    try:
        conv_id = int(block.get("conversation_id") or 0)
    except (TypeError, ValueError):
        conv_id = 0
    try:
        debounce = float(block.get("debounce_seconds", DEFAULT_DEBOUNCE_SECONDS))
    except (TypeError, ValueError):
        debounce = DEFAULT_DEBOUNCE_SECONDS
    debounce = max(DEBOUNCE_MIN, min(DEBOUNCE_MAX, debounce))
    types = block.get("types")
    if (
        isinstance(types, list)
        and types
        and all(isinstance(t, str) and t for t in types)
    ):
        types = list(types)
    else:
        types = None
    prompt = str(block.get("prompt") or "").strip()
    return {
        "enabled": bool(block.get("enabled", False)),
        "path": str(block.get("path") or block.get("file") or "").strip(),
        "conversation_id": conv_id,
        "debounce_seconds": debounce,
        "types": types,
        "prompt": prompt,
    }


def tail_new_lines(path: str, offset: int) -> tuple[list[str], int]:
    """Complete JSON lines appended since byte `offset`, plus the new
    offset. Byte offsets on purpose (binary read + decode): text-mode
    seek() with a byte count breaks on multibyte characters mid-file.
    A trailing partial line waits for its rest — the next poll finishes
    it. errors="replace" guards a torn UTF-8 sequence at the seam."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return [], offset
    if not data:
        return [], offset
    if not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        if cut < 0:
            return [], offset  # one partial line, no newline yet
        data = data[: cut + 1]
    return data.decode("utf-8", errors="replace").splitlines(), offset + len(data)


class WakeFileWatcher:
    """Tails one journal file and coalesces its actionable events into
    wakes of one target conversation. The parsing half (handle_line /
    tail_new_lines) is deliberately side-effect-free so tests can drive
    lines through without a conversation or a clock."""

    def __init__(
        self,
        path: str,
        conversation_id: int,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
        types: list[str] | None = None,
        prompt: str = "",
        offset: int | None = None,
    ):
        self.path = path
        self.conversation_id = conversation_id
        self.debounce_seconds = max(DEBOUNCE_MIN, min(DEBOUNCE_MAX, debounce_seconds))
        # None = the default noise-blocklist (everything but heartbeats);
        # an explicit list is a whitelist override.
        self.types = list(types) if types is not None else None
        self.custom_prompt = prompt.strip()
        try:
            self._size = os.path.getsize(path)
        except OSError:
            self._size = 0
        # Boot forgets history: without an explicit offset we start at the
        # file's current end (no catch-up on launch, like the scheduler).
        self.offset = self._size if offset is None else offset
        self._pending_count = 0
        self._last_action_at: float | None = None

    def handle_line(self, raw: str) -> str | None:
        """The event type if this line should wake the chat, else None.
        Unparseable lines never wake anything: a torn or foreign journal
        must not turn into prompt spam. Default mode is a NOISE
        blocklist (everything but heartbeats wakes the chat — the chat
        reads and answers unknown types itself); an explicit config
        `types` list narrows it to a whitelist."""
        raw = raw.strip()
        if not raw:
            return None
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        etype = str(event.get("type") or "")
        if self.types is not None:
            return etype if etype in self.types else None
        if not etype or etype in DEFAULT_NOISE:
            return None
        return etype

    def prompt_for(self, count: int) -> str:
        """The wake prompt: names the journal and the contract — read the
        new events, answer each per its own _instructions, with the same
        CLI a hand-driven live session uses. Config `prompt` replaces it
        wholesale when set."""
        if self.custom_prompt:
            return self.custom_prompt
        return (
            f"[wake] {count} new event(s) in the live-session journal "
            f"{self.path}. Read the file tail, take the events since your "
            "last reply, and act on each exactly as its _instructions "
            "field directs (generate/steer/accept replies via the "
            "impeccable CLI). When no unhandled events remain, end the "
            "turn."
        )

    async def poll_once(self) -> None:
        """One look at the file: absorb new lines, fire when the debounce
        window has passed with pending events. Fires enqueue when the
        target chat is mid-turn (the queue drains at the next step
        boundary) — never a second concurrent stream."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            self._size = 0
            return
        if self._size and size < self._size:
            # Truncated or rotated: start over instead of seeking past EOF.
            self.offset = 0
        self._size = size
        lines, self.offset = tail_new_lines(self.path, self.offset)
        for raw_line in lines:
            if self.handle_line(raw_line):
                self._pending_count += 1
                self._last_action_at = time.monotonic()
        if (
            self._pending_count
            and self._last_action_at is not None
            and time.monotonic() - self._last_action_at >= self.debounce_seconds
        ):
            count, self._pending_count = self._pending_count, 0
            self._last_action_at = None
            await _fire_wake(self.conversation_id, self.prompt_for(count))


async def _fire_wake(conversation_id: int, prompt: str) -> str:
    """Start (or queue) one wake turn. Returns 'started' | 'queued' |
    'gone'. Runs drain in a background task the module holds a strong
    ref to — a wake run is fire-and-forget, like a scheduler fire."""
    from backend.agent import loop as loop_mod

    if loop_mod.agent_is_running(conversation_id):
        loop_mod.enqueue_message(conversation_id, prompt)
        return "queued"
    conv = await get_conversation(conversation_id)
    if conv is None:
        log.warning("wake target conversation %s is gone", conversation_id)
        return "gone"
    # The target is a user chat: its pinned model/effort/sub-agent scope
    # lives on the conversation row. Resolution is owned by backend.main
    # (the same call an interactive send uses); the scheduler never needs
    # it because agent rows carry their own scope columns. Deferred import
    # — main imports this module at the lifespan, never the reverse at
    # load time.
    from backend.main import _resolve_turn_scope

    model, effort, subagent_scope = _resolve_turn_scope(conv)

    async def _drain() -> None:
        try:
            async for line in loop_mod.run_agent(
                conversation_id,
                prompt,
                conv.get("workspace") or "",
                model_override=model,
                effort_override=effort,
                subagent_scope=subagent_scope,
            ):
                # Idle->busy race: the target started a turn between our
                # busy-check and the claim. run_agent refuses with a
                # non-fatal `kind: busy` event — recover the wake through
                # the queue instead of dropping the events on the floor
                # (the queue drains at the running turn's next boundary).
                if '"kind": "busy"' in line or '"kind":"busy"' in line:
                    loop_mod.enqueue_message(conversation_id, prompt)
                    return
        except Exception:  # noqa: BLE001 — a failed wake must not kill the watcher
            log.exception("wake-fired run on conversation %s failed", conversation_id)

    task = asyncio.create_task(_drain())
    _run_tasks.add(task)
    task.add_done_callback(_run_tasks.discard)
    return "started"


async def _watch_loop() -> None:
    try:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            watcher = _watcher
            if watcher is None:
                continue
            try:
                await watcher.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — keep the watch alive
                log.exception("wake poll failed")
    except asyncio.CancelledError:
        raise


def start_wake_watcher() -> bool:
    """Start the tail loop if the config enables a complete wake block.
    Returns whether a watcher is now running. Incomplete config logs and
    stays off — a half-typed block must not fire turns at conversation 0."""
    global _watcher, _task
    if _task is not None and not _task.done():
        return True
    block = read_wake_config()
    if not block["enabled"]:
        return False
    if not block["path"] or block["conversation_id"] <= 0:
        log.warning(
            "wake block incomplete (path=%r conversation_id=%r); not starting",
            block["path"], block["conversation_id"],
        )
        return False
    _watcher = WakeFileWatcher(
        path=block["path"],
        conversation_id=block["conversation_id"],
        debounce_seconds=block["debounce_seconds"],
        types=block["types"],
        prompt=block["prompt"],
    )
    _task = asyncio.create_task(_watch_loop())
    log.info(
        "wake watcher: %s -> conversation %s (debounce %.1fs, types %s)",
        block["path"], block["conversation_id"],
        block["debounce_seconds"], block["types"] or "default",
    )
    return True


def stop_wake_watcher() -> None:
    global _watcher, _task
    if _task is not None and not _task.done():
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            pass
    _task = None
    _watcher = None
    # Runs already in flight finish naturally: the per-conversation run
    # lock owns them, and killing a started turn would strand the chat's
    # transcript mid-run.
