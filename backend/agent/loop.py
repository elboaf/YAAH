"""The agent loop: model <-> tools cycle with streaming events.

Runs until the model produces a final answer or the step budget is exhausted.
Emits JSON-line events for the frontend:
  {'type': 'text', 'text': ...}             - assistant text delta
  {'type': 'thinking', 'text': ...}         - model reasoning delta (UI-only)
  {'type': 'model_call', 'provider', 'model'} - chat call dispatched, awaiting
                                             first response byte (issue #43)
  {'type': 'tool_start', 'name', 'args'}    - tool execution beginning
  {'type': 'tool_progress', 'call_id', 'chunk'} - live shell output while a tool runs
  {'type': 'tool_result', 'name', 'result'} - tool output
  {'type': 'done'}                          - final answer complete
  {'type': 'error', 'message'}              - fatal error
"""
import asyncio
import itertools
import json
import os
from pathlib import Path
from typing import AsyncIterator

from backend.agent import model_client
from backend.agent.config import load_config, save_config
from backend.agent.imagedata import load_data_url
from backend.agent import skills as skill_registry
from backend.agent import subagents as subagents_mod
from backend.agent import worktrees
from backend.agent.tools import execute_tool, get_schemas, tool_risk, workspace_root
from backend.agent.remote import CMD_TOOLS_NOTE
from backend.db.database import (
    add_message,
    get_conversation,
    get_messages,
    set_conversation_usage,
)

DEFAULT_MAX_STEPS = 200
MAX_TOOL_RESULT_CHARS = 20_000
MAX_AGENTS_NOTES_CHARS = 8_000

# The env line's shell dialect must match what create_subprocess_shell
# actually spawns — COMSPEC on Windows (near-universally cmd.exe), $SHELL
# on POSIX. Saying "bash" on a cmd host (or vice versa) costs the model a
# turn per Unix reflex (ls, grep, tail) before it falls back to findstr.
def _shell_phrase(windows: bool) -> str:
    if windows:
        shell = Path(os.environ.get("COMSPEC") or "cmd.exe").name.lower()
        phrase = f"the system shell ({shell})"
        if "cmd" in shell:
            phrase += f"; {CMD_TOOLS_NOTE}"
        return phrase
    shell = Path(os.environ.get("SHELL") or "bash").name
    return f"the system shell ({shell})"


def _local_env_line() -> str:
    import platform

    return (
        f"Runtime environment: {platform.system()} {platform.release()} "
        f"({platform.machine()}). The bash tool runs commands through "
        f"{_shell_phrase(os.name == 'nt')}; use commands and paths valid "
        f"for THIS operating system."
    )


def _agents_notes(workspace: str) -> str:
    """Project-notes block from <workspace>/AGENTS.md, or ''. Skipped for
    remote workspaces (the file lives on the host, unreadable here) and
    swallowed on read errors — optional context must never break a turn."""
    try:
        root = workspace_root(workspace)
        if not root.is_dir():
            return ""
        text = (root / "AGENTS.md").read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — missing/unreadable notes are fine
        return ""
    if not text.strip():
        return ""
    if len(text) > MAX_AGENTS_NOTES_CHARS:
        text = text[:MAX_AGENTS_NOTES_CHARS] + "\n…[truncated]"
    return (
        f"# Project notes ({root / 'AGENTS.md'})\n\n"
        "The project's own instructions for coding agents follow; they "
        "override the general guidance below where they conflict.\n\n"
        f"{text}"
    )


def _computer_use_prompt() -> str:
    """Computer-use section for the system prompt (Windows local only).
    General principles only — tool mechanics live in the tool schemas,
    depth in the bundled computer-use skill. Kept deliberately lean:
    hyper-specific rules accrete per dogfood session and go stale."""
    from backend.agent import computer as computer_mod

    return f"""

Computer use (desktop tools):
- These tools exist for TESTING apps: launch the app under test via the
  shell tools, find it with list_windows, then drive and verify its UI.
  Do not move the user's mouse or type into windows outside the task.
- These move the USER'S REAL mouse and keyboard. For an app running
  inside the Windows Sandbox they are forbidden — the sandbox has its
  own input session; use in-VM AutoHotkey via sandbox_run instead
  (see the sandbox section). Host input here is only for apps running
  on the host itself.
- Prefer shell/file tools for anything reachable that way; computer use
  is for GUI behavior you must observe or exercise.
- Structured first, pixels second: read_ui_tree gives exact element
  names, values and center coordinates — prefer it for locating
  controls and verifying state; fall back to screenshots when the tree
  is empty or useless.
- COORDINATE CONTRACT: pixel coordinates from a screenshot are
  MONITOR-LOCAL — pass them to mouse tools with that monitor number,
  never converted by hand. Screenshots carry labeled coordinate
  rulers: click values read off a ruler, never visually estimated
  positions.
- CORRECTIONS come from measurement: after a miss, use the observe
  crop or a region screenshot to measure the delta and adjust once.
  Re-guessing from the full screen, or repeating the same coordinates,
  is a failure pattern, not persistence.
- Honor what tool results tell you: ok:false notes and warnings are
  instructions. If a result says "user-activity pause", the user is at
  the machine — wait a few seconds and retry when idle.
- VERIFY outcomes against a baseline: state claims need before/after
  evidence, weak indicators are hints rather than conclusions, and two
  checks that don't settle it mean report what you know and ask.
  Prefer state-independent actions over toggles, and never toggle
  state you haven't verified.
- Be decisive: most desktop requests are 2-4 actions (find, focus,
  act, verify). When a request is done, say so; when it can't be
  completed, say that instead of wandering.
- mcp_* tools come from connected tool servers. When one matches the
  task, prefer it — structured tool calls beat GUI automation every
  time; fall back to the desktop tools only for what no server covers.
- Screenshot only when the task requires seeing the screen — never to
  inspect the user's other work. Screenshots go to the model provider.
- {computer_mod.panic_notice()}"""


def _default_system_prompt(workspace: str = "") -> str:
    """SYSTEM_PROMPT adapted to the current EXECUTION TARGET: the tool list
    and the runtime-environment line must match what execute_tool can
    actually do where tools run — the remote host while one is connected,
    otherwise this machine — or the model attempts commands for the wrong
    platform (e.g. PowerShell registry queries on Linux). The chat's
    workspace rides along so a remote session can name the selected folder
    on the host instead of always claiming the host's home."""
    from backend.agent import remote as remote_mod

    host = remote_mod.get_remote()
    if host is not None:
        env = host.env_line(workspace)
        windows = host.windows
    else:
        windows = os.name == "nt"
        env = _local_env_line()
    tools = ["bash (shell commands)"]
    computer_section = ""
    sandbox_section = ""
    if windows:
        tools.append("powershell (Windows PowerShell)")
        if host is None:
            # Computer use always drives THIS machine (never forwarded to a
            # remote host), so the section only appears without a host.
            tools += [
                "screenshot", "list_windows", "focus_window",
                "read_ui_tree (structured UI elements of a window — prefer "
                "this over screenshots for locating controls)",
                "mouse_move", "mouse_click", "mouse_drag", "mouse_scroll",
                "type_text", "press_key", "wait",
            ]
            computer_section = _computer_use_prompt()
            # Same rule: the sandbox integration drives THIS machine's
            # disposable VMs, so it's only offered in local sessions.
            from backend.agent import sandbox as sandbox_mod

            tools += [
                "sandbox_test (boot/reuse a disposable Windows Sandbox VM "
                "— the default place to run the workspace's app/tests)",
                "sandbox_run (run a command inside that VM)",
                "sandbox_status", "sandbox_stop",
            ]
            sandbox_section = sandbox_mod.prompt_section()
    tools += [
        "web_search", "web_fetch", "view_image", "read_file", "write_file",
        "create_file", "edit_file", "delete_file", "move_file",
        "search_files",
        "git tools (git_status, git_diff, git_add, git_commit, git_push, git_pull)",
        "spawn_agent (delegate self-contained work to a sub-agent; see the "
        "sub-agents index below)",
    ]
    prompt = f"""You are an expert AI coding agent working inside a user's project workspace.

{env}

You have tools: {", ".join(tools)}.

Guidelines:
- Explore before acting: use search_files and read files before editing.
- Prefer edit_file for targeted changes; write_file only for new files or full rewrites.
- read_file returns line ranges: page through large files with start_line/end_line.
- Verify your work INSIDE the sandbox when the work is disruptive:
  full test suites, builds, installs, servers, anything that writes
  outside the workspace. Quick smoke checks — a single test
  file, a typecheck, a lint pass, an import — may run host-side
  when they are read-only and stay inside the workspace (smoke-test
  rule). Boot the VM with sandbox_test and run commands via
  sandbox_run. Size the timeout to the command; a full test suite
  that takes minutes needs a large timeout_seconds or chunked runs
  (per directory/file), not retries. The VM is a clean image:
  install missing tools into the toolkit (installs persist across
  sandboxes).
- Sandbox work stays IN the sandbox: every dependency the app under test
  needs (runtimes, browsers, portable tools) is installed into the VM's
  toolkit — never launch a host equivalent (e.g. the host browser) to
  exercise the app, and never drive the app's GUI with the host
  mouse/keyboard tools; in-VM AutoHotkey is the input layer for that.
- If a full-suite verification fails, separate YOUR change from the
  environment: rerun just the failing tests at a clean tree (git stash, or
  a throwaway `git worktree add` at HEAD) and diff the failure lists
  before assuming your change caused them.
- Write isolation is automatic (#58): your first write rebinds you to
  your own git worktree — edit/verify there freely; at end of turn the
  harness merges your committed work back. Commit what you want kept
  BEFORE finishing: uncommitted changes are refused and salvaged, never
  merged. Shell commands run with cwd inside your worktree.
- For web research, start with web_search and read pages with web_fetch;
  use view_image on an image URL you actually need to see.
- Commit meaningful work with git_add/git_commit when the user asks for it.
- Narrate as you work: open with one or two lines on what you're about to
  do, say what a tool call or delegation is for before making it, and
  comment on what came back before deciding the next step. Short, plain
  lines — the user is watching the stream, and a long silence reads as a
  hang. Never hold all your prose back for one final dump.
- Paths are relative to the workspace root.

Spoken briefing (voice read-aloud):
- If the user has read-aloud enabled, your words are SPOKEN, not read
  verbatim. End EVERY text emission with a <say> tag containing a 1-3
  sentence spoken briefing of what just happened — under 400 characters,
  plain prose, no markdown, no lists, no code. That includes each interim
  text block you emit between tool calls while a run is in progress (the
  voice keeps pace with the run), not only the final answer.
  Write what the user most needs to hear: the outcome and the next step.
  The tag is stripped from the chat transcript; the chat text itself stays
  as detailed as you like. Omit the tag when read-aloud is off or when the
  emission is a question (ask_user questions are spoken verbatim already).

Interview the user (ask_user tool):
- Do not make assumptions about a plan, decision, or idea. Put each
  decision to the user with ask_user and wait for the answer.
- Finding facts is your job, never the user's: never ask for anything
  you could look up yourself with tools.
- Don't block on unsettled exploration: a running exploration is an
  unsettled prerequisite, so only the questions downstream of it wait
  for the exploration to report. Decisions wait; facts don't.
- The session is done when nothing is left silently assumed. Do not
  act on a decision until the user has confirmed shared understanding."""

    prompt += computer_section
    prompt += sandbox_section

    # Skills index: only added when at least one model-invocable skill
    # exists, so a fresh install with no skills sees no extra noise.
    skill_index = skill_registry.index_for_prompt()
    if skill_index:
        prompt += "\n\n" + skill_index

    # Sub-agent index: the parent needs to know what it can delegate to.
    prompt += "\n\n" + subagents_mod.index_for_prompt()
    return prompt

# Per-conversation cancellation flags checked between model/tool steps.
_cancel_events: dict[int, asyncio.Event] = {}

# Conversation ids with a turn in flight. A second concurrent run on the
# same conversation would clobber its _cancel_events entry and interleave
# two streams into one chat, so it is rejected.
_running_convs: set[int] = set()


def agent_is_running(conversation_id: int) -> bool:
    return conversation_id in _running_convs


def try_begin_run(conversation_id: int) -> bool:
    """Atomically claim a conversation for a run; False if already claimed."""
    if conversation_id in _running_convs:
        return False
    _running_convs.add(conversation_id)
    return True

# Pending ask_user calls: "conversation_id:call_id" -> Future carrying the
# user's answer text. Resolved by the /answer API endpoint.
_pending_answers: dict[str, asyncio.Future] = {}


def resolve_answer(conversation_id: int, call_id: str, answer: str) -> bool:
    """Deliver a user answer to a pending ask_user call or approval request.
    Returns False when
    no question is waiting (e.g. the run already ended or was stopped)."""
    fut = _pending_answers.get(f"{conversation_id}:{call_id}")
    if fut is None or fut.done():
        return False
    fut.set_result(answer)
    return True


async def _wait_answer(
    conversation_id: int, call_id: str, cancel_ev: asyncio.Event
) -> asyncio.Future:
    """Register a pending-answer future and block until the user resolves it
    (POST /answer) or the run is cancelled. The stream stays open and other
    conversations keep running. Raises on cancel so callers can emit their
    own not-answered result; the future's result is the raw answer string."""
    key = f"{conversation_id}:{call_id}"
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_answers[key] = fut
    cancel_task = asyncio.create_task(cancel_ev.wait())
    try:
        done, _ = await asyncio.wait(
            {fut, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if fut in done:
            return fut
        raise asyncio.CancelledError("run stopped before answer")
    finally:
        cancel_task.cancel()
        _pending_answers.pop(key, None)
        if not fut.done():
            fut.cancel()


async def _ask_user(
    conversation_id: int, call_id: str, args: dict, cancel_ev: asyncio.Event
) -> dict:
    """Block the loop until the user answers (or the run is cancelled).
    The stream stays open and other conversations keep running."""
    try:
        fut = await _wait_answer(conversation_id, call_id, cancel_ev)
    except asyncio.CancelledError:
        return {"answer": None, "note": "user did not answer (run stopped)"}
    return {"answer": fut.result()}


def cancel_agent(conversation_id: int):
    """Request cancellation of a running agent turn for this conversation."""
    ev = _cancel_events.get(conversation_id)
    if ev is not None:
        ev.set()


# ---- Message queue + steering (issue #7) ------------------------------------
# Messages typed while a run is in flight, held server-side per conversation
# (in-memory, same lifetime as the run itself). The loop drains the queue at
# step boundaries - soft injection: the run continues with full context. A
# steer interrupts the current in-flight gated tool so the boundary arrives
# NOW. Endings: natural completion and errors auto-send whatever is left
# (queued meant "this matters regardless"); a hard Stop holds the queue.
_message_queues: dict[int, list[dict]] = {}
_steer_flags: dict[int, asyncio.Event] = {}
_queue_seq = 0


def enqueue_message(
    conversation_id: int,
    text: str,
    skills: list[str] | None = None,
) -> dict:
    """Queue a user message for the running conversation; returns the item."""
    global _queue_seq
    _queue_seq += 1
    item = {"id": _queue_seq, "text": text, "skills": skills or []}
    _message_queues.setdefault(conversation_id, []).append(item)
    return item


def queue_items(conversation_id: int) -> list[dict]:
    return list(_message_queues.get(conversation_id, []))


def remove_queued(conversation_id: int, item_id: int) -> bool:
    q = _message_queues.get(conversation_id) or []
    for it in q:
        if it["id"] == item_id:
            q.remove(it)
            return True
    return False


def steer_agent(conversation_id: int) -> bool:
    """Interrupt the current in-flight step so the next boundary lands now.
    False when the conversation has no run to steer."""
    ev = _steer_flags.get(conversation_id)
    if ev is None:
        return False
    ev.set()
    return True


def _drain_queue(conversation_id: int) -> list[dict]:
    items = _message_queues.get(conversation_id) or []
    if items:
        _message_queues[conversation_id] = []
    return items


async def _take_injections(conversation_id: int) -> list[dict]:
    """Drain queued messages at a step boundary and persist each as a user
    row (the transcript reads chronologically; replay feeds them to the
    model in order). Caller emits the user_injected events and appends the
    in-memory messages entries."""
    items = _drain_queue(conversation_id)
    for it in items:
        await add_message(conversation_id, "user", it["text"])
    return items


# ---- access-mode gate (PLAN-access-modes.md) -------------------------------

_VALID_MODES = ("ask", "plan", "full")

# Answers the approval card sends through the /answer endpoint. Anything
# else typed into the free-text box is a deny-with-guidance for the model.
_APPROVE = "approve"
_DENY = "deny"


def current_access_mode() -> str:
    """The configured access mode, validated. Read fresh at every gate so a
    header switch applies to the next tool call of a running turn."""
    mode = (load_config().get("access_mode") or "ask").lower()
    return mode if mode in _VALID_MODES else "ask"


def _plan_mode_note() -> str:
    """System-prompt section injected while plan mode is active."""
    return (
        "# Access mode: PLAN\n\n"
        "Plan mode is ON: file edits and shell commands are blocked, so do "
        "not attempt them. Explore, then present your plan by calling the "
        "exit_plan tool with the plan as its `plan` argument \u2014 the user "
        "approves it (plan mode ends and you continue executing in the same "
        "run) or sends change requests for you to incorporate. Do NOT just "
        "write the plan as text and stop: without an exit_plan call the user "
        "has no way to approve it."
    )


def _sandbox_only_note() -> str:
    """System-prompt section injected for scheduled agents running with the
    sandbox-only approval policy (issue #41): no user is watching, so
    approval-required tools never execute."""
    return (
        "# Scheduled agent: sandbox-only policy\n\n"
        "This is an unattended scheduled run: every tool that normally "
        "requires user approval (file edits, shell commands and anything "
        "else mutating) is unavailable — calls come back as \"skipped: "
        "approval required\". Do "
        "not attempt them or retry after a skip. Work read-only: gather "
        "information, check status, and report findings, keeping anything "
        "disruptive as a recommendation for the user to run themselves."
    )


# Only injected while plan mode is active (run_agent_turn below); in every
# other mode the model has no exit_plan to call.
EXIT_PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "exit_plan",
        "description": (
            "Present your plan for approval while in plan mode. Blocks until "
            "the user responds: approval ends plan mode and the SAME run "
            "continues straight into execution; anything else comes back as "
            "feedback for you to incorporate and re-present. Call this "
            "instead of writing the plan as plain text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "The full plan for the user to review",
                },
            },
            "required": ["plan"],
        },
    },
}


def _policy_skip_result(name: str) -> dict:
    # The exact "skipped: approval required" wording is part of the issue #41
    # contract: the model reads it, notes what it would have done, moves on.
    return {
        "error": (
            "skipped: approval required — this scheduled agent runs under the "
            f"sandbox-only policy, so {name} cannot execute. Continue with "
            "read-only work and note what you would have done."
        )
    }


def _plan_block_result(name: str) -> dict:
    return {
        "error": (
            f"plan mode is on: {name} was not executed. Present your plan "
            "with the exit_plan tool and wait for approval."
        )
    }


async def _exit_plan(
    conversation_id: int, call_id: str, args: dict, cancel_ev: asyncio.Event
) -> dict:
    """Block the loop until the user approves the plan, requests changes, or
    the run is cancelled. Approval flips the saved access mode to full BEFORE
    the future resolves, so the gate passes on the very next tool call of
    this same turn."""
    if current_access_mode() != "plan":
        return {"error": "exit_plan is only available in plan mode"}
    plan = str(args.get("plan") or "").strip()
    if not plan:
        return {"error": "exit_plan requires a non-empty `plan` argument"}
    try:
        fut = await _wait_answer(conversation_id, call_id, cancel_ev)
    except asyncio.CancelledError:
        return {"decision": "cancelled", "note": "user did not answer (run stopped)"}
    answer = fut.result()
    if answer == "approve":
        save_config({"access_mode": "full"})
        return {"decision": "approved", "note": "plan approved; plan mode is OFF — execute the plan now"}
    # Free text = a change request; stay in plan mode and let the model revise.
    return {"decision": "revised", "feedback": answer}


async def _await_approval(
    conversation_id: int, call_id: str, name: str, cancel_ev: asyncio.Event
) -> dict | None:
    """Ask mode: block until the user approves or denies this tool call
    (same future machinery as ask_user; the frontend renders an approval
    card). None = approved; dict = denial error result for the model."""
    key = f"{conversation_id}:{call_id}"
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_answers[key] = fut
    cancel_task = asyncio.create_task(cancel_ev.wait())
    try:
        done, _ = await asyncio.wait(
            {fut, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if fut in done:
            answer = fut.result()
            if answer == _APPROVE:
                return None
            if answer == _DENY:
                return {"error": f"user denied the {name} tool call"}
            # Free-text answer: a denial carrying guidance for the model.
            return {"error": f"user denied the {name} tool call: {answer}"}
        return {"error": "user did not answer the approval request (run stopped)"}
    finally:
        cancel_task.cancel()
        _pending_answers.pop(key, None)
        if not fut.done():
            fut.cancel()


async def run_gate(
    name: str,
    args: dict,
    call_id: str,
    conversation_id: int,
    cancel_ev: asyncio.Event,
    mode: str | None = None,
    emit=None,
) -> dict | None:
    """Access-mode decision for one tool call, shared by the main loop and
    the sub-agent runner. Returns None to execute, or an error-result dict
    to use instead of executing. `mode` may be passed in by a caller that
    already read it (the main loop reads once per call so its streamed
    request event and this decision cannot disagree). `emit` (optional)
    receives the approval_request / approval_decision events in order —
    used by the sub-agent runner, which forwards them to the parent stream.

    - read tools and full mode: pass through.
    - plan mode + mutating/shell: never executes; returns the plan-mode
      error immediately (the prompt already told the model to plan).
    - ask mode + mutating/shell: block on the user's decision.
    """
    risk = tool_risk(name)
    if mode is None:
        mode = current_access_mode()
    if risk == "read" or mode == "full":
        return None
    if mode == "plan":
        return _plan_block_result(name)
    if emit:
        emit(
            {
                "type": "approval_request",
                "name": name,
                "args": args,
                "call_id": call_id,
            }
        )
    result = await _await_approval(conversation_id, call_id, name, cancel_ev)
    if emit:
        emit(
            {
                "type": "approval_decision",
                "name": name,
                "call_id": call_id,
                "approved": result is None,
            }
        )
    return result


async def _load_skill(
    args: dict,
    loaded_skills: list[str],
    messages: list,
) -> dict:
    """Handle the model's load_skill call. The real logic lives in
    skills.load_skill_into_messages so the sub-agent runner shares it."""
    return skill_registry.load_skill_into_messages(args, loaded_skills, messages)


def _cancelled(conversation_id: int) -> bool:
    ev = _cancel_events.get(conversation_id)
    return bool(ev and ev.is_set())


def _ndjson(event: dict) -> str:
    return json.dumps(event) + "\n"


async def _execute_with_progress(
    name: str, args: dict, workspace: str, call_id: str, box: dict,
    cancel_ev: asyncio.Event | None = None,
    steer_ev: asyncio.Event | None = None,
):
    """Run a tool, yielding tool_progress events with live output while it
    runs (only the shell executors actually stream; everything else emits
    nothing and behaves like a plain await). The final result lands in
    `box["result"]` because async-generator return values are awkward to
    consume alongside `async for`. When `cancel_ev` fires mid-tool the tool
    task is killed and the result is discarded — Stop must not have to wait
    out a long-running command; the turn loop sees the cancelled event next
    and ends the turn."""
    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    def on_chunk(text: str) -> None:
        queue.put_nowait(text)

    task = asyncio.create_task(execute_tool(name, args, workspace, on_chunk=on_chunk))

    async def _finisher():
        try:
            await task
        finally:
            queue.put_nowait(_DONE)

    finisher = asyncio.create_task(_finisher())
    # Races queue.get() against the Stop button so a cancelled tool unwinds
    # immediately instead of at its natural end.
    cancel_waiter = (
        asyncio.create_task(cancel_ev.wait()) if cancel_ev is not None else None
    )
    steer_waiter = (
        asyncio.create_task(steer_ev.wait()) if steer_ev is not None else None
    )
    cancelled = False
    steered = False
    try:
        while True:
            get_task = asyncio.create_task(queue.get())
            waiters = [get_task]
            if cancel_waiter:
                waiters.append(cancel_waiter)
            if steer_waiter:
                waiters.append(steer_waiter)
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if cancel_waiter is not None and cancel_waiter in done:
                get_task.cancel()
                cancelled = True
                break
            if steer_waiter is not None and steer_waiter in done:
                get_task.cancel()
                cancelled = True
                steered = True
                break
            item = get_task.result()
            if item is _DONE:
                break
            yield {"type": "tool_progress", "call_id": call_id, "chunk": item}
        if cancelled:
            # A steer (#7) interrupts the tool but NOT the turn: the result
            # tells the model the user stepped in; the caller continues to
            # the injection boundary. A plain Stop (cancel_ev) still ends
            # the turn.
            if not task.done():
                task.cancel()
            await asyncio.gather(task, finisher, return_exceptions=True)
            box["result"] = (
                {
                    "error": (
                        "interrupted by the user (steered): the user has queued a "
                        "message that will be delivered now - expect it next."
                    )
                }
                if steered
                else {"error": "tool stopped by user (run cancelled)"}
            )
            box["steered"] = steered
            return
        await finisher
        box["result"] = task.result()
    finally:
        if cancel_waiter is not None:
            cancel_waiter.cancel()
        if steer_waiter is not None:
            steer_waiter.cancel()
        # If the turn is torn down mid-tool (client disconnect, Stop),
        # don't leak a running tool task the way a bare create_task would.
        if not task.done():
            task.cancel()
            await asyncio.gather(task, finisher, return_exceptions=True)


def _clip_result_str(result, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Serialize a tool result, staying under `limit` WITHOUT producing
    invalid JSON: the longest string field is clipped instead of slicing
    the whole dump mid-token (a hard slice feeds the model garbage)."""
    s = json.dumps(result)
    if len(s) <= limit:
        return s
    if isinstance(result, dict):
        clipped = dict(result)
        str_keys = [k for k, v in clipped.items() if isinstance(v, str)]
        if str_keys:
            key = max(str_keys, key=lambda k: len(clipped[k]))
            clipped[key] = clipped[key][: max(limit - 200, 0)] + "…[truncated]"
            clipped["note"] = "output truncated to fit the context window"
            out = json.dumps(clipped)
            if len(out) <= limit:
                return out
    return json.dumps({"note": "output truncated", "head": s[:limit]})


def _image_part(data_url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": data_url}}


def _parts_with_images(text: str, image_rels: list) -> list | str:
    """Build an OpenAI multimodal parts list (text + image_url parts) from
    stored image rel paths. Falls back to plain text when none of the
    files can be read (deleted/moved)."""
    parts: list = []
    if text:
        parts.append({"type": "text", "text": text})
    for rel in image_rels:
        data_url = load_data_url(rel)
        if data_url:
            parts.append(_image_part(data_url))
        else:
            parts.append({"type": "text", "text": f"[image file missing: {rel}]"})
    return parts or text


async def load_history(conversation_id: int) -> list:
    """Load persisted messages back into OpenAI chat format."""
    rows = await get_messages(conversation_id)
    out = []
    # Track which assistant tool_call ids actually made it into the replayed
    # history, so we can drop orphaned 'tool' rows (e.g. when a malformed
    # assistant tool_calls payload was stripped above). Most OpenAI-compatible
    # APIs reject tool messages with no preceding assistant tool_calls.
    valid_call_ids: set[str] = set()
    answered_call_ids: set[str] = set()
    for r in rows:
        role = r["role"]
        if role == "user":
            content = r["content"]
            if r.get("images"):
                content = _parts_with_images(content, r["images"])
            out.append({"role": "user", "content": content})
        elif role == "assistant":
            m = {"role": "assistant", "content": r["content"]}
            tcs = r.get("tool_calls")
            # Only replay well-formed OpenAI tool calls (id + function.name)
            if tcs and isinstance(tcs[0], dict) and tcs[0].get("id") and tcs[0].get("function"):
                m["tool_calls"] = tcs
                valid_call_ids.update(
                    tc.get("id", "") for tc in tcs if tc.get("id")
                )
            out.append(m)
        elif role == "tool":
            # Prefer the dedicated tool_call_id column; fall back to the
            # legacy convention of stashing the id in the tool_calls column.
            tc_id = r.get("tool_call_id")
            if not tc_id:
                meta = (r.get("tool_calls") or [{}])[0]
                tc_id = meta.get("id", "")
            if tc_id and tc_id not in valid_call_ids:
                continue  # orphaned tool result; skip to keep history valid
            answered_call_ids.add(tc_id)
            content = r["content"]
            if r.get("images"):
                # tool-attached image (view_image): replay as parts so a
                # vision model still sees it on resume
                try:
                    text = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    text = content
                if not isinstance(text, str):
                    text = json.dumps(text.get("note", "[image]"))
                content = _parts_with_images(text, r["images"])
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id or "",
                    "content": content,
                }
            )
        # system rows skipped; we inject our own system prompt fresh each turn
    # Tool calls that never got a result (e.g. the app closed while an
    # ask_user question was pending) must still be answered or most
    # OpenAI-compatible APIs reject the replayed history.
    for tc_id in valid_call_ids - answered_call_ids:
        out.append(
            {
                "role": "tool",
                "tool_call_id": tc_id,
                "content": json.dumps(
                    {"answer": None, "note": "not answered (session ended)"}
                ),
            }
        )
    return _prune_old_images(out)


# Providers cap vision inputs (and price every one of them): a long
# computer-use session would otherwise replay dozens of full screenshots
# on every turn. Keep the most recent KEEP_RECENT_IMAGES images intact;
# older ones become a text placeholder (the tool result's text survives).
KEEP_RECENT_IMAGES = 6


def _prune_old_images(messages: list, keep: int = KEEP_RECENT_IMAGES) -> list:
    seen = 0
    for m in reversed(messages):
        content = m.get("content")
        if not isinstance(content, list):
            continue
        has_image = False
        for i, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "image_url":
                has_image = True
                seen += 1
                if seen > keep:
                    content[i] = {
                        "type": "text",
                        "text": "[older screenshot pruned from context]",
                    }
        # a parts-list whose images were all pruned still has its text part
        if not has_image and len(content) == 1 and isinstance(content[0], dict):
            m["content"] = content[0].get("text", "")
    return messages


async def run_agent(
    conversation_id: int,
    user_text: str,
    workspace: str,
    image_paths: list | None = None,
    skill_names: list | None = None,
    persist_user: bool = True,
    policy: str | None = None,
    include_history: bool = True,
    model_override: str = "",
    effort_override: str = "",
) -> AsyncIterator[str]:
    """Execute one user turn. Yields JSON-line event strings.

    image_paths: rel paths (under backend/data/images/) of images the user
    attached; already saved to disk by the API layer.
    skill_names: skills the user invoked with /s or a chip; their bodies
    are injected into the system prompt for this turn only.
    persist_user: False when resuming an interrupted turn — the user
    message is already stored and must not be duplicated.
    policy: per-run approval policy for scheduled agents (issue #41),
    "sandbox-only" (gated tools skip with a note) or "autonomous"
    (everything auto-approved); None = normal chat turn driven by the
    global access mode.
    include_history: False = fresh context each fire (the agent's memory
    toggle off) — the system prompt is built, but prior transcript rows
    are not replayed into model context.
    model_override / effort_override: the agent's per-agent model +
    reasoning effort; blank = the active global settings."""
    # Persist the user message first (skipped on resume; the text still
    # reaches the model through the replayed history below).
    if not try_begin_run(conversation_id):
        yield _ndjson({
            "type": "error",
            "message": "a turn is already running in this conversation",
        })
        return

    # Issue #58: nothing is created up front — the shared workspace stays
    # untouched until the first write-capable tool call (read-only turns
    # never pay for isolation). `turn_workspace` is the (possibly rebound)
    # workspace every tool call and spawn_batch sees from then on.
    turn_workspace = str(workspace)
    _isolated = False
    _merge_result: dict | None = None

    if persist_user:
        await add_message(
            conversation_id, "user", user_text, images=image_paths or None
        )

    # Per-conversation system prompt override (Q17) wins over the global one
    conv = await get_conversation(conversation_id)
    system_prompt = (conv or {}).get("system_prompt_override") or _default_system_prompt(workspace)

    # Explicitly invoked skills (/s name or a chip): their instruction
    # bodies are appended to the system prompt for THIS turn only — never
    # persisted, so later turns don't replay them.
    invoked = [n for n in (skill_names or []) if isinstance(n, str) and n.strip()]
    if invoked:
        skill_block = skill_registry.bodies_for_prompt(invoked)
        if skill_block:
            system_prompt = f"{system_prompt}\n\n---\n\n# Invoked skills\n\n{skill_block}"

    # The project's own agent instructions (baseline failures, shell quirks,
    # prerequisites) travel with the workspace, so read them fresh each turn.
    notes = _agents_notes(workspace)
    if notes:
        system_prompt = f"{system_prompt}\n\n---\n\n{notes}"

    # Plan mode tells the model what it cannot do, so it plans instead of
    # hitting blocked-tool errors all turn.
    if current_access_mode() == "plan":
        system_prompt = f"{system_prompt}\n\n---\n\n{_plan_mode_note()}"

    # Scheduled-run policy note: same idea — say up front what is off-limits
    # so the model doesn't burn steps discovering it via skip errors.
    if policy == "sandbox-only":
        system_prompt = (
            f"{system_prompt}\n\n---\n\n{_sandbox_only_note()}"
        )

    # Full context each turn: system prompt + persisted history. An agent
    # with its memory toggle off (issue #41) gets a fresh context instead —
    # the transcript still exists in the chat, it just doesn't feed the
    # model, so the current turn's text is appended explicitly (it only
    # reaches the model through the replayed history otherwise).
    history = await load_history(conversation_id) if include_history else []
    messages = [{"role": "system", "content": system_prompt}] + history
    if not include_history:
        messages.append({"role": "user", "content": user_text})

    tools = get_schemas()
    # exit_plan exists only while plan mode is on (the schema is how the
    # model learns it can ask for approval at all).
    if current_access_mode() == "plan":
        tools = tools + [EXIT_PLAN_SCHEMA]
    cancel_ev = asyncio.Event()
    _cancel_events[conversation_id] = cancel_ev
    steer_ev = asyncio.Event()
    _steer_flags[conversation_id] = steer_ev

    # Skills the model has loaded mid-turn via load_skill (deduped, order
    # preserved). Their bodies are appended to the system prompt so every
    # subsequent model call in this turn sees them.
    loaded_skills: list[str] = []

    # Per-turn step budget; 0 or blank means unlimited (Stop button still ends
    # the turn). Configured in Settings → Max steps or config.json `max_steps`.
    try:
        max_steps = int(load_config().get("max_steps") or 0)
    except (TypeError, ValueError):
        max_steps = DEFAULT_MAX_STEPS

    try:
        for _step in range(max_steps) if max_steps > 0 else itertools.count():
            if cancel_ev.is_set():
                yield _ndjson({"type": "stopped", "reason": "cancelled by user"})
                return

            # The retry must wrap the whole stream consumption, not just the
            # chat() call: with stream=True the HTTP request only fires when
            # iteration starts, so a ModelError surfaces at the first chunk.
            state: dict = {"content": "", "tool_calls": None, "finish": None}

            async def _model_step() -> AsyncIterator[str]:
                """One chat call, consumed to completion, yielding UI event
                lines. Results land in `state`; raises ModelError on API
                failure (including mid-stream drops)."""
                state["content"] = ""
                state["tool_calls"] = None
                state["finish"] = None
                state["usage"] = None
                acc: list[str] = []
                # <say> suppression (#66): the briefing tag may stream as
                # ordinary text deltas. A tail buffer holds back the last
                # chunk of a possible "<say>...</say>" so the tag never
                # reaches the UI or the transcript; whatever is not part of
                # a complete tag is flushed verbatim.
                tail = ""
                say_open = False
                say_buf = ""
                # Per-agent model/effort overrides only ride along when an
                # agent actually set them, so the default call path (and
                # anything patching chat with the base signature) is
                # unchanged.
                if model_override or effort_override:
                    stream = await model_client.chat(
                        messages,
                        tools=tools,
                        stream=True,
                        model=model_override,
                        effort=effort_override,
                    )
                else:
                    stream = await model_client.chat(messages, tools=tools, stream=True)
                async for ev in stream:
                    if cancel_ev.is_set():
                        return
                    if ev["type"] == "model_call":
                        # Issue #43: the chat call is dispatched and nothing
                        # has come back yet — surface who we're waiting on.
                        yield _ndjson(
                            {
                                "type": "model_call",
                                "provider": ev.get("provider") or "",
                                "model": ev.get("model") or "",
                            }
                        )
                    elif ev["type"] == "content":
                        acc.append(ev["text"])
                        tail += ev["text"]
                        if not say_open:
                            i = tail.find("<say>")
                            if i >= 0:
                                # Tag opens: flush what precedes it, hold the rest.
                                if i > 0:
                                    yield _ndjson({"type": "text", "text": tail[:i]})
                                say_open = True
                                say_buf = tail[i + 5 :]
                                tail = ""
                            elif "<say" in tail[-12:]:
                                # Possible partial "<say" split across chunks:
                                # hold it back until the next chunk decides.
                                k = tail.rindex("<say")
                                yield _ndjson({"type": "text", "text": tail[:k]})
                                tail = tail[k:]
                            else:
                                yield _ndjson({"type": "text", "text": tail})
                                tail = ""
                        else:
                            j = say_buf.find("</say>")
                            if j >= 0:
                                # Tag closed: the briefing stays in acc (it is
                                # stripped from the transcript below) but is
                                # never streamed to the UI.
                                say_open = False
                                tail = say_buf[j + 6 :]
                                say_buf = ""
                            # else: still inside the tag, hold everything.
                    elif ev["type"] == "thinking":
                        # Model reasoning stream: UI-only (telemetry tape),
                        # never stored in the transcript.
                        yield _ndjson({"type": "thinking", "text": ev["text"]})
                    elif ev["type"] == "tool_calls":
                        state["tool_calls"] = ev["tool_calls"]
                    elif ev["type"] == "finish":
                        state["finish"] = ev.get("reason")
                    elif ev["type"] == "usage":
                        state["usage"] = ev.get("usage")
                # Flush the tail: whatever is held back at stream end that is
                # not inside an (unclosed) tag is chat text.
                if tail and not say_open:
                    yield _ndjson({"type": "text", "text": tail})
                state["content"] = "".join(acc)

            attempt = 0
            while True:
                attempt += 1
                try:
                    async for line in _model_step():
                        yield line
                    break
                except model_client.ModelError as e:
                    if attempt >= 2 or cancel_ev.is_set():
                        raise
                    # Auto-recovery (Q23): retry once with corrective context.
                    # Text already streamed before the failure may repeat in
                    # the UI — cosmetic next to losing the whole turn.
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                f"Your previous request failed with: {e}. "
                                "Retry with a corrected request."
                            ),
                        }
                    )

            if cancel_ev.is_set():
                yield _ndjson({"type": "stopped", "reason": "cancelled by user"})
                return

            # Exact context size: usage.prompt_tokens is what THIS call fed
            # the model (system + history + tool plumbing). Persist per call —
            # the last call of a turn is the fullest — without touching
            # updated_at (a readout must not re-sort the session list).
            usage = state.get("usage") or {}
            if usage.get("prompt_tokens") is not None:
                _model_id = load_config().get("model") or None
                asyncio.create_task(
                    set_conversation_usage(
                        conversation_id, int(usage["prompt_tokens"]), _model_id
                    )
                )

            # Two-channel split (#66): the <say> briefing is speech-only —
            # it is stripped from the stored transcript, and the spoken line
            # ships as its own `say` event (never as chat text). Fallbacks
            # (heuristic first/last-sentence line, then the old truncated
            # verbatim read) live in speak.spoken_line; the frontend plays
            # `say` when present and degrades to the old path otherwise.
            from backend.agent import speak as _speak

            assistant_content = state["content"]
            assistant_content, said = _speak.extract_say(assistant_content)
            tool_calls = state["tool_calls"]
            finish_reason = state["finish"]

            # Persist assistant message (with tool calls if any)
            await add_message(
                conversation_id,
                "assistant",
                assistant_content,
                tool_calls=tool_calls,
            )

            # Briefing-first, per emission (#66): EVERY completed model
            # emission with content speaks — mid-run emissions included — so
            # the voice channel keeps pace with the run instead of waiting
            # for the final answer. spoken_line falls back (heuristic, then
            # clipped verbatim) when the emission carried no usable <say>
            # tag. Text-less emissions (a bare tool_calls message) say
            # nothing rather than emitting an empty briefing.
            _said = _speak.spoken_line(said, assistant_content)
            if _said:
                yield _ndjson({"type": "say", "text": _said})

            # No tool calls => final answer; turn complete
            if not tool_calls:
                if finish_reason == "length":
                    yield _ndjson(
                        {
                            "type": "text",
                            "text": "\n\n[output truncated: the model hit its "
                            "max output tokens — raise max_tokens in Settings]",
                        }
                    )
                # Final usage readout for the UI's context strip (the last
                # model call of the turn = the peak context this turn used).
                if usage.get("prompt_tokens") is not None:
                    yield _ndjson(
                        {
                            "type": "usage",
                            "prompt_tokens": usage["prompt_tokens"],
                            "model": load_config().get("model") or None,
                        }
                    )
                # Natural-completion auto-send (#7): anything still queued
                # rides home as a queued_autosend hand-off before done.
                remaining = _drain_queue(conversation_id)
                if remaining:
                    yield _ndjson({"type": "queued_autosend", "items": remaining})
                yield _ndjson({"type": "done"})
                return

            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_content,
                    "tool_calls": tool_calls,
                }
            )

            # Steer boundary (#7): a steer with no tool calls in flight
            # (the model went straight to tools / the step ended early)
            # lands here - before the next model call. Same persistence
            # + announcement as the tool-result boundary.
            for inj in await _take_injections(conversation_id):
                yield _ndjson(
                    {"type": "user_injected", "text": inj["text"], "id": inj["id"]}
                )
                messages.append({"role": "user", "content": inj["text"]})

            # Partition this step's tool calls: spawn_agent delegations run
            # in parallel (foreground — the parent blocks until all finish);
            # every other tool stays sequential (v1 contract).
            spawn_calls = [tc for tc in tool_calls if tc["function"]["name"] == "spawn_agent"]
            regular_calls = [tc for tc in tool_calls if tc["function"]["name"] != "spawn_agent"]

            # Execute each regular tool call in order
            for tc in regular_calls:
                if cancel_ev.is_set():
                    yield _ndjson({"type": "stopped", "reason": "cancelled by user"})
                    return
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError as e:
                    if finish_reason == "length":
                        # The model hit its output cap mid-arguments; telling
                        # it "invalid JSON" makes it retry the identical call
                        # and truncate again (the reread-same-file loop).
                        result = {
                            "error": (
                                "Tool arguments were cut off because the model "
                                "reached its max output tokens (finish_reason="
                                "length). Retry with a much shorter call — e.g. "
                                "smaller arguments or a narrower file range — "
                                "or ask the user to raise max_tokens in Settings."
                            )
                        }
                    else:
                        result = {"error": f"Invalid JSON arguments: {e}"}
                else:
                    yield _ndjson(
                        {
                            "type": "tool_start",
                            "name": name,
                            "args": args,
                            "call_id": tc.get("id", ""),
                        }
                    )
                    if name == "ask_user":
                        if policy == "sandbox-only":
                            # No user is watching a scheduled run; blocking on
                            # an answer that may never come would wedge the
                            # turn until max_steps.
                            result = {
                                "answer": None,
                                "note": (
                                    "skipped: approval required — no user is "
                                    "available to answer (scheduled sandbox-only "
                                    "run); decide yourself or note the open "
                                    "question in your report"
                                ),
                            }
                        else:
                            result = await _ask_user(
                                conversation_id, tc.get("id", ""), args, cancel_ev
                            )
                    elif name == "exit_plan":
                        result = await _exit_plan(
                            conversation_id, tc.get("id", ""), args, cancel_ev
                        )
                    elif name == "load_skill":
                        result = await _load_skill(args, loaded_skills, messages)
                    else:
                        # Access-mode gate (PLAN-access-modes.md). The mode is
                        # read ONCE here and passed in, so the request event
                        # the loop yields and the decision the gate enforces
                        # cannot disagree. approval_request must stream
                        # BEFORE the gate blocks: a generator cannot yield
                        # while it is awaiting the user's answer.
                        mode = current_access_mode()
                        # A scheduled run's approval policy overrides the
                        # global mode (issue #41): autonomous auto-approves
                        # everything (never emits a gate — the approval
                        # deadlock must be unreachable unattended);
                        # sandbox-only skips gated tools entirely (below).
                        if policy == "autonomous":
                            mode = "full"
                        if tool_risk(name) == "read" or mode == "full":
                            # Issue #58 rebinding seam: before the first
                            # write-capable call, rebind the turn to its own
                            # worktree (refusal surfaces as the tool's error
                            # result — never a dead turn).
                            _refused = False
                            if tool_risk(name) != "read" and not _isolated:
                                try:
                                    turn_workspace = await worktrees.ensure_isolated(
                                        workspace, chat_id=str(conversation_id)
                                    )
                                    _isolated = True
                                    workspace = turn_workspace
                                except worktrees.IsolationRefused as e:
                                    result = {"error": str(e)}
                                    _refused = True
                            if not _refused:
                                box: dict = {}
                                async for pev in _execute_with_progress(
                                    name, args, turn_workspace, tc.get("id", ""), box,
                                    cancel_ev=cancel_ev, steer_ev=steer_ev,
                                ):
                                    yield _ndjson(pev)
                                result = box.get("result")
                        elif policy == "sandbox-only":
                            result = _policy_skip_result(name)
                        else:
                            if mode == "ask":
                                yield _ndjson(
                                    {
                                        "type": "approval_request",
                                        "name": name,
                                        "args": args,
                                        "call_id": tc.get("id", ""),
                                    }
                                )
                            result = await run_gate(
                                name,
                                args,
                                tc.get("id", ""),
                                conversation_id,
                                cancel_ev,
                                mode=mode,
                            )
                            if mode == "ask":
                                yield _ndjson(
                                    {
                                        "type": "approval_decision",
                                        "name": name,
                                        "call_id": tc.get("id", ""),
                                        "approved": result is None,
                                    }
                                )
                            # Approved (None) -> execute for real now; a
                            # denial/plan-block carries its own error result.
                            if result is None:
                                # Issue #58 rebinding seam (gated path):
                                if tool_risk(name) != "read" and not _isolated:
                                    try:
                                        turn_workspace = await worktrees.ensure_isolated(
                                            workspace, chat_id=str(conversation_id)
                                        )
                                        _isolated = True
                                        workspace = turn_workspace
                                    except worktrees.IsolationRefused as e:
                                        result = {"error": str(e)}
                                if result is None:
                                    box = {}
                                    async for pev in _execute_with_progress(
                                        name, args, turn_workspace, tc.get("id", ""), box,
                                        cancel_ev=cancel_ev, steer_ev=steer_ev,
                                    ):
                                        yield _ndjson(pev)
                                    result = box.get("result")

                # Steering (#7): a steer interrupt lands the queued message
                # NOW. Replace the tool result's error text, clear the flag,
                # and skip the old cancel check below so the run continues
                # to the injection boundary. `box` only exists on the paths
                # that ran _execute_with_progress; the gated-tool path that
                # never reached execution carries its result dict directly.
                _steered_here = bool(box.get("steered")) if "box" in dir() else False
                if _steered_here:
                    steer_ev.clear()
                elif steer_ev.is_set():
                    steer_ev.clear()
                    result = {
                        "error": (
                            "interrupted by the user (steered): the user has queued "
                            "a message that will be delivered now - expect it next."
                        )
                    }

                result_str = _clip_result_str(result)
                # A tool that attached an image (view_image) becomes a
                # multimodal parts list for the live LLM call.
                image_rel = result.get("image") if isinstance(result, dict) else None
                tool_content = result_str
                image_data_url = load_data_url(image_rel) if image_rel else None
                if image_data_url:
                    tool_content = [
                        {"type": "text", "text": result_str},
                        _image_part(image_data_url),
                    ]
                yield _ndjson(
                    {
                        "type": "tool_result",
                        "name": name,
                        "result": result,
                        "image": image_rel,
                        "call_id": tc.get("id", ""),
                    }
                )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": tool_content,
                    }
                )
                # Persist tool result; store call id + name in tool_calls column
                await add_message(
                    conversation_id,
                    "tool",
                    result_str,
                    tool_calls=[{"id": tc.get("id", ""), "name": name}],
                    tool_call_id=tc.get("id", ""),
                    images=[image_rel] if image_rel else None,
                )

                # Steer boundary (#7): the queued message lands after the
                # interrupted (or completed) tool result, before the next
                # model call. Injected rows are real user turns - persisted,
                # replayed from history, announced to the UI.
                for inj in await _take_injections(conversation_id):
                    yield _ndjson(
                        {"type": "user_injected", "text": inj["text"], "id": inj["id"]}
                    )
                    messages.append({"role": "user", "content": inj["text"]})

            # Run every spawn_agent delegation of this step in parallel.
            # Progress events flow through a queue so the generator can
            # yield them live while the batch task runs.
            if spawn_calls and not cancel_ev.is_set():
                calls = []
                for i, tc in enumerate(spawn_calls):
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    calls.append(
                        {
                            "call_id": tc.get("id", ""),
                            "agent_id": i,
                            "agent_type": args.get("agent_type"),
                            "prompt": args.get("prompt"),
                            "args": args,
                        }
                    )
                    yield _ndjson(
                        {
                            "type": "tool_start",
                            "name": "spawn_agent",
                            "args": {"agent_type": calls[-1]["agent_type"], "prompt": calls[-1]["prompt"]},
                            "call_id": tc.get("id", ""),
                        }
                    )

                queue: asyncio.Queue = asyncio.Queue()

                def _emit(ev: dict) -> None:
                    queue.put_nowait(ev)

                # Sub-agent tool calls pass through the same access-mode
                # gate: approval requests ride the batch's event queue to
                # the stream, and the user's decision resolves the same
                # future map (keyed by the sub-agent's own tool call id).
                # A scheduled run's policy applies to its sub-agents too:
                # sandbox-only skips gated tools; autonomous passes None
                # straight through (auto-approve, no gate events).
                if policy == "sandbox-only":

                    def _sub_gate(name: str, args: dict, call_id: str):
                        return asyncio.sleep(0, result=_policy_skip_result(name))  # type: ignore[arg-type]

                elif policy == "autonomous":

                    def _sub_gate(name: str, args: dict, call_id: str):
                        return asyncio.sleep(0, result=None)  # type: ignore[arg-type]

                else:

                    def _sub_gate(name: str, args: dict, call_id: str):
                        return run_gate(
                            name,
                            args,
                            call_id,
                            conversation_id,
                            cancel_ev,
                            emit=_emit,
                        )

                batch = asyncio.create_task(
                    subagents_mod.spawn_batch(
                        calls, turn_workspace, cancel_ev, on_event=_emit, gate=_sub_gate
                    )
                )

                async def _persist_spawn_result(tc: dict, result: dict) -> tuple:
                    """Persist one spawn_agent result row: summary in the
                    tool content, full transcript in its own column.
                    Returns (summary, result_str) for the parent context."""
                    transcript = result.get("transcript")
                    summary = {k: v for k, v in result.items() if k != "transcript"}
                    result_str = _clip_result_str(summary)
                    await add_message(
                        conversation_id,
                        "tool",
                        result_str,
                        tool_calls=[{"id": tc.get("id", ""), "name": "spawn_agent"}],
                        tool_call_id=tc.get("id", ""),
                        sub_agent_transcript=summary | {"transcript": transcript},
                    )
                    return summary, result_str

                try:
                    while True:
                        getter = asyncio.create_task(queue.get())
                        done, _ = await asyncio.wait(
                            {getter, batch}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if getter in done:
                            yield _ndjson(getter.result())
                        if batch in done:
                            getter.cancel()
                            break
                    while not queue.empty():
                        yield _ndjson(queue.get_nowait())
                    batch_results = batch.result()
                except (asyncio.CancelledError, GeneratorExit):
                    # The turn is dying while sub-agents are in flight
                    # (Stop aborted the stream, client disconnected).
                    # Give the batch a grace window to shut down via the
                    # cancel event — each sub-agent then returns a
                    # 'cancelled' result with its partial transcript —
                    # and persist whatever came back, so an interrupted
                    # run leaves a record instead of vanishing. Awaits
                    # are legal here; only yields are forbidden.
                    cancel_ev.set()
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(batch),
                            timeout=subagents_mod.SPAWN_GRACE_SECONDS,
                        )
                    except BaseException:  # noqa: BLE001 — grace expired
                        pass
                    try:
                        partial = batch.result()
                    except BaseException:  # noqa: BLE001 — hard-cancelled
                        partial = {}
                    for tc in spawn_calls:
                        result = partial.get(tc.get("id", "")) or {
                            "status": "cancelled",
                            "output": "",
                            "note": "run interrupted before completion",
                        }
                        try:
                            await asyncio.shield(
                                _persist_spawn_result(tc, result)
                            )
                        except BaseException:  # noqa: BLE001 — best effort
                            pass
                    raise
                finally:
                    # Last resort: a batch that ignored the grace window
                    # (e.g. a wedged subprocess) must not leak detached.
                    if not batch.done():
                        batch.cancel()

                for tc in spawn_calls:
                    result = batch_results.get(
                        tc.get("id", ""), {"error": "sub-agent produced no result"}
                    )
                    summary, result_str = await _persist_spawn_result(tc, result)
                    yield _ndjson(
                        {
                            "type": "tool_result",
                            "name": "spawn_agent",
                            "result": summary,
                            "call_id": tc.get("id", ""),
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": result_str,
                        }
                    )

        budget_msg = (
            f"Step budget ({max_steps}) exhausted — raise it in "
            "Settings → Max steps (or config.json `max_steps`; 0 = unlimited)"
        )
        await add_message(conversation_id, "system", f"turn failed: {budget_msg}")
        yield _ndjson({
            "type": "error",
            "message": budget_msg,
        })

        # Steering hand-off (#7): drain a POST-run boundary first. Whatever
        # remains belongs to the ending: natural completion and errors
        # auto-send (queued meant "this matters regardless"), a hard Stop
        # holds the queue so explicit silence wins.
        for inj in await _take_injections(conversation_id):
            yield _ndjson(
                {"type": "user_injected", "text": inj["text"], "id": inj["id"]}
            )
        remaining = _drain_queue(conversation_id)
        if remaining and not cancel_ev.is_set():
            yield _ndjson({"type": "queued_autosend", "items": remaining})

    except model_client.ModelError as e:
        # The user message is already stored; without a record of the
        # failure the transcript would read as if the turn never happened.
        await add_message(conversation_id, "system", f"turn failed: {e}")
        yield _ndjson({"type": "error", "message": str(e)})
    except Exception as e:  # noqa: BLE001
        await add_message(
            conversation_id, "system", f"turn failed: {type(e).__name__}: {e}"
        )
        yield _ndjson({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        # Issue #58 decision 5: a top-level chat agent self-merges at end
        # of turn — under the merge mutex, whatever exit the turn took
        # (done, error, cancelled). A generator's finally may not resume
        # arbitrarily deep awaits on abort, so the merge is wrapped in
        # shield+wait_for; refusal/cancellation marks the worktree for the
        # reaper (salvage-before-delete), nothing is ever lost silently.
        if _isolated:
            try:
                _merge_result = await asyncio.wait_for(
                    asyncio.shield(worktrees.self_merge(str(conversation_id))),
                    timeout=60,
                )
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                _merge_result = {
                    "merged": False,
                    "reason": "self-merge did not complete; worktree left for the reaper",
                }
            if not _merge_result.get("noop"):
                await add_message(
                    conversation_id,
                    "system",
                    json.dumps({"worktree_merge": _merge_result}, default=str),
                )
                yield _ndjson(
                    {
                        "type": "tool_result",
                        "name": "git_merge_back",
                        "call_id": f"merge-back-{conversation_id}",
                        "result": _merge_result,
                    }
                )
        _cancel_events.pop(conversation_id, None)
        _steer_flags.pop(conversation_id, None)
        _running_convs.discard(conversation_id)