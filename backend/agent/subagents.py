"""Sub-agent framework: registry, definitions, and the nested runner.

A sub-agent is a nested agent loop with its own fresh context. The parent
model delegates work by calling the ``spawn_agent`` tool; the sub-agent's
final message returns as the tool result, so the parent's context only
ever holds the summary, never the sub-agent's intermediate tool calls.

Registry model (mirrors the skills system):
  - two built-ins: ``general-purpose`` (full tools) and ``explore``
    (read-only research);
  - user-defined agents as markdown files under ~/.yaah/agents/ with
    YAML frontmatter (name, description, tools, disallowedTools,
    maxTurns, model) and the body as the system prompt.

Execution contract (v1):
  - foreground only: every spawn_agent call in one parent turn runs in
    parallel (capped) and the parent blocks until all finish;
  - no nesting: a sub-agent cannot spawn sub-agents;
  - no ask_user: a sub-agent must decide for itself and report the
    assumption in its final message;
  - isolated writes (issue #58): sub-agents read the parent's folder;
    the first write-capable tool call rebinds a writing sub-agent to
    its own git worktree — the branch is reported on the first line of
    the result and the parent/chat merges it (sub-agents never merge);
  - parent cancellation cancels children; a child failure returns a
    structured error result and never kills the parent's turn.
"""
import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.agent import model_client
from backend.agent.config import load_config
from backend.agent import skills as skill_registry
from backend.agent import worktrees
from backend.agent.tools import execute_tool, get_schemas, workspace_root

# ------------------------------------------------------------------ registry

AGENTS_DIR = Path(
    os.environ.get("YAAH_AGENTS_PATH") or Path.home() / ".yaah" / "agents"
)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)

DEFAULT_MAX_TURNS = 50
MAX_AGENTS = 100
# Simultaneous sub-agents per parent turn. Foreground-parallel means the
# parent is blocked while these run; 4 keeps a runaway fan-out from
# monopolizing the model endpoint while still covering real partitions.
MAX_CONCURRENT = 4
# A sub-agent's final message becomes the parent's tool result, so it is
# clipped by the same budget as any other tool output.
MAX_RESULT_CHARS = 20_000
MAX_TRANSCRIPT_CHARS = 200_000
# When the parent turn dies mid-batch (Stop aborted the stream, client
# disconnected), the batch gets this many seconds to shut down via the
# cancel event and return partial results before it is hard-cancelled.
SPAWN_GRACE_SECONDS = 10.0


@dataclass
class AgentDef:
    name: str
    description: str
    body: str
    tools: list[str] | None = None          # None = inherit all
    disallowed_tools: list[str] = field(default_factory=list)
    max_turns: int = DEFAULT_MAX_TURNS
    model: str | None = None                # parsed but deferred (v1.1)
    builtin: bool = False


# Tool sets for the built-ins. Sub-agents never get ask_user (they cannot
# block on the user), spawn_agent (no nesting), or computer-use tools (two
# agents cannot share one mouse/keyboard).
_ALWAYS_EXCLUDED = {"ask_user", "spawn_agent"}
_COMPUTER_TOOLS = {
    "screenshot", "list_windows", "focus_window", "read_ui_tree",
    "mouse_move", "mouse_click", "mouse_drag", "mouse_scroll",
    "type_text", "press_key", "wait",
}

_EXPLORE_TOOLS = {
    "read_file", "search_files", "web_search", "web_fetch", "view_image",
    "git_status", "git_diff", "load_skill",
}


def _builtin_general_purpose() -> AgentDef:
    return AgentDef(
        name="general-purpose",
        description=(
            "The default sub-agent for broad tasks: implement a small "
            "feature, fix a clear issue, run verification commands, or "
            "carry a self-contained piece of work forward in isolation. "
            "Has access to all tools except ask_user and spawn_agent."
        ),
        body=(
            "You are a focused sub-agent working inside a larger task. "
            "Complete the delegated work independently: explore what you "
            "need, make the changes, verify them, and report a concise "
            "final summary. Your final message is the ONLY thing the "
            "parent agent receives — make it self-contained: what you "
            "did, what you changed (paths), what you verified, and any "
            "assumptions or follow-ups."
        ),
        builtin=True,
    )


def _builtin_explore() -> AgentDef:
    return AgentDef(
        name="explore",
        description=(
            "Read-only codebase research specialist: broad code search, "
            "call-chain investigation, architecture discovery, and "
            "evidence gathering. Cannot create, modify, move, or delete "
            "files. Prefer this over general-purpose when the task only "
            "needs reading and analysis."
        ),
        body=(
            "You are a read-only research sub-agent. Investigate the "
            "codebase or web thoroughly and report findings with file "
            "paths and line references as evidence. You cannot and must "
            "not modify anything. Your final message is the ONLY thing "
            "the parent agent receives — make it a complete, "
            "self-contained report of what you found."
        ),
        tools=sorted(_EXPLORE_TOOLS),
        builtin=True,
    )


_BUILTINS: dict[str, AgentDef] = {
    d.name: d for d in (_builtin_general_purpose(), _builtin_explore())
}

# name -> AgentDef; rebuilt by scan_agents(), read by everything else.
_agents: dict[str, AgentDef] = {}
_scanned = False


def _yaml_load(raw: str) -> object:
    try:
        import yaml

        return yaml.safe_load(raw)
    except ImportError:
        out: dict = {}
        for line in raw.splitlines():
            if ":" in line and not line.strip().startswith("#"):
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip().strip("'\"")
        return out


def parse_agent_md(path: Path) -> AgentDef | None:
    """Parse one agent definition file. Returns None when the file is not
    a valid definition (missing/blank name) so a broken file never breaks
    the whole scan."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    m = _FRONTMATTER_RE.match(text)
    meta: dict = {}
    body = text
    if m:
        try:
            loaded = _yaml_load(m.group(1))
        except Exception:  # noqa: BLE001 — bad YAML: definition is unusable
            return None
        if isinstance(loaded, dict):
            meta = loaded
        body = text[m.end():]

    name = str(meta.get("name") or "").strip()
    if not name:
        return None
    try:
        max_turns = int(meta.get("maxTurns") or DEFAULT_MAX_TURNS)
    except (TypeError, ValueError):
        max_turns = DEFAULT_MAX_TURNS
    max_turns = max(1, min(max_turns, 500))

    def _str_list(v) -> list[str]:
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        if isinstance(v, list):
            return [str(p).strip() for p in v if str(p).strip()]
        return []

    return AgentDef(
        name=name,
        description=str(meta.get("description") or "").strip(),
        body=body.strip(),
        tools=_str_list(meta.get("tools")) or None,
        disallowed_tools=_str_list(meta.get("disallowedTools")),
        max_turns=max_turns,
        model=str(meta.get("model") or "").strip() or None,
    )


def scan_agents() -> dict[str, AgentDef]:
    """Rescan AGENTS_DIR and replace the cache. Idempotent; never raises.
    Built-ins always win over a user file with the same name."""
    global _agents, _scanned
    found: dict[str, AgentDef] = {}
    if AGENTS_DIR.is_dir():
        try:
            entries = sorted(AGENTS_DIR.iterdir())
        except OSError:
            entries = []
        for child in entries[: MAX_AGENTS * 4]:
            if len(found) >= MAX_AGENTS:
                break
            if child.is_file() and child.suffix == ".md":
                agent = parse_agent_md(child)
                if agent is not None and agent.name not in _BUILTINS:
                    found[agent.name] = agent
    _agents = found
    _scanned = True
    return found


def ensure_scanned() -> dict[str, AgentDef]:
    if not _scanned:
        scan_agents()
    return _agents


def list_agents() -> list[dict]:
    """All agent definitions for the UI / system-prompt index."""
    ensure_scanned()
    out = [
        {
            "name": d.name,
            "description": d.description,
            "tools": d.tools,
            "disallowed_tools": d.disallowed_tools,
            "max_turns": d.max_turns,
            "builtin": d.builtin,
        }
        for d in _BUILTINS.values()
    ]
    out += [
        {
            "name": d.name,
            "description": d.description,
            "tools": d.tools,
            "disallowed_tools": d.disallowed_tools,
            "max_turns": d.max_turns,
            "builtin": False,
        }
        for d in sorted(_agents.values(), key=lambda d: d.name.lower())
    ]
    return out


def get_agent_def(name: str) -> AgentDef | None:
    ensure_scanned()
    return _BUILTINS.get(name) or _agents.get(name)


def index_for_prompt() -> str:
    """The <available_subagents> block for the parent's system prompt.
    Empty string when only the built-ins exist (they are always present,
    so the block is never empty — but kept as a function for symmetry)."""
    lines = [
        "Sub-agents available (delegate with the spawn_agent tool):",
    ]
    for d in list_agents():
        desc = " ".join(d["description"].split())
        lines.append(f"- {d['name']}: {desc}")
    lines.append(
        "Delegate proactively: when a chunk of work is self-contained — "
        "a sweep over many files, independent research threads, broad "
        "exploration that would eat this conversation's context — prefer "
        "spawning a sub-agent over doing it inline. Launch several "
        "spawn_agent calls in the SAME turn to run them in parallel "
        "instead of one long sequential investigation. Announce each "
        "delegation in one line before the calls, and summarize what "
        "came back when the results arrive. Write each sub-agent prompt "
        "fully self-contained (it sees nothing else from this "
        "conversation — include paths, constraints, and exactly what to "
        "report back). Do not delegate what needs back-and-forth with "
        "the user, and don't fragment one small edit into delegation."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ runner


def _resolve_tools(defn: AgentDef, windows: bool) -> list[dict]:
    """Tool schemas for a sub-agent: built-ins minus always-excluded and
    computer-use, then the definition's allow/deny lists applied."""
    schemas = get_schemas()
    allowed: dict[str, dict] = {}
    for s in schemas:
        n = s["function"]["name"]
        if n in _ALWAYS_EXCLUDED or n in _COMPUTER_TOOLS:
            continue
        allowed[n] = s
    if defn.tools is not None:
        allowed = {n: s for n, s in allowed.items() if n in set(defn.tools)}
    for n in defn.disallowed_tools:
        allowed.pop(n, None)
    return list(allowed.values())


def _sub_agent_system_prompt(defn: AgentDef, workspace: str) -> str:
    """System prompt for a sub-agent run: its definition body plus the
    same environment grounding the parent gets (env line, workspace
    notes, skills index) so commands and paths are valid for the host."""
    from backend.agent import remote as remote_mod
    from backend.agent.loop import _local_env_line, _agents_notes

    host = remote_mod.get_remote()
    if host is not None:
        # Tools forward to the host, so the sub-agent needs the HOST's
        # environment grounding (OS/shell/workspace), not this machine's.
        windows = host.windows
        env = host.env_line(workspace)
    else:
        windows = os.name == "nt"
        env = _local_env_line()
    tools = ["bash (shell commands)"]
    if windows:
        tools.append("powershell (Windows PowerShell)")
    tools += [
        "web_search", "web_fetch", "view_image", "read_file", "write_file",
        "create_file", "edit_file", "delete_file", "move_file",
        "search_files",
        "git tools (git_status, git_diff, git_add, git_commit, git_push, git_pull)",
    ]
    prompt = (
        f"You are a sub-agent (agent_type: {defn.name}) spawned by a "
        f"parent agent. {defn.body}\n\n"
        f"{env}\n\n"
        f"You have tools: {', '.join(tools)}.\n\n"
        "Guidelines:\n"
        "- You cannot ask the user questions: decide for yourself, act on "
        "the most reasonable interpretation, and report the assumption in "
        "your final message.\n"
        "- You cannot spawn sub-agents of your own.\n"
        "- Your final message is the only thing the parent receives. Make "
        "it self-contained: what you did, what you changed (paths), what "
        "you verified, and any follow-ups.\n"
        "- The user sees your streamed text live in the parent's transcript. "
        "Lead with a one-line summary of what you're doing, then work.\n"
        "- Paths are relative to the workspace root.\n"
    )
    notes = _agents_notes(workspace)
    if notes:
        prompt += f"\n\n---\n\n{notes}"
    skill_index = skill_registry.index_for_prompt()
    if skill_index:
        prompt += f"\n\n---\n\n{skill_index}"
    return prompt


async def run_sub_agent(
    defn: AgentDef,
    prompt: str,
    workspace: str,
    cancel_ev: asyncio.Event | None = None,
    on_event=None,
    gate=None,
    run_label: str = "",
) -> dict:
    """Run one sub-agent to completion. Returns the tool-result dict for
    the parent: final message, status, and a transcript snapshot.

    Never raises. A model error becomes status='error' with the message;
    cancellation becomes status='cancelled' with whatever was produced.
    `gate` is the parent loop's access-mode coroutine factory
    (loop.make_gate): the sub-agent's tool calls pass through the same
    approval gate as the parent's, so delegation cannot launder
    permissions.
    """
    cancel_ev = cancel_ev or asyncio.Event()
    # Issue #58: the sub-agent starts on the workspace it was handed (the
    # parent's tree, or the parent's own worktree for nested fan-out). The
    # first write-capable tool call rebinds `run_workspace` to this
    # agent's own worktree; every tool call goes through _exec so file,
    # shell, and git tools all follow the rebinding in one place.
    run_workspace = str(workspace)
    _isolation_note: str | None = None

    async def _exec(name: str, args: dict) -> dict:
        nonlocal run_workspace, _isolation_note, _used_worktree
        if _used_worktree is None and name in worktrees.WRITER_TRIGGERS:
            try:
                run_workspace = await worktrees.ensure_isolated(
                    run_workspace, chat_id=run_label or f"sub-{id(defn):x}"
                )
            except worktrees.IsolationRefused as e:
                _isolation_note = str(e)
                return {"error": str(e)}
            else:
                _used_worktree = run_workspace
        return await execute_tool(name, args, run_workspace)

    _used_worktree: str | None = None

    _used_worktree: str | None = None
    messages = [
        {"role": "system", "content": _sub_agent_system_prompt(defn, workspace)},
        {"role": "user", "content": prompt},
    ]
    tools = _resolve_tools(defn, windows=os.name == "nt")
    loaded_skills: list[str] = []
    transcript: list[dict] = []

    def _record(role: str, content, **extra):
        entry = {"role": role, "content": content, **extra}
        transcript.append(entry)
        return entry

    _record("user", prompt)

    final_text = ""
    status = "completed"
    error: str | None = None
    turns = 0
    grace_note: str | None = None

    try:
        # One grace turn past the budget: when the loop exhausts max_turns
        # without a final answer, the model gets exactly one more call with
        # tools still available, told to converge NOW — so an explore-heavy
        # run can still write its deliverable instead of dying on tool calls.
        grace = False
        for turn in range(max(defn.max_turns, 1)):
            turns = turn + 1
            if cancel_ev.is_set():
                status = "cancelled"
                break

            # Budget awareness: near the end of the budget, tell the model
            # to start converging (it cannot course-correct on a budget it
            # does not know exists).
            remaining = defn.max_turns - turns
            if (
                not grace
                and remaining >= 0
                and remaining < max(3, defn.max_turns // 5)
            ):
                if remaining == 0:
                    note = (
                        "This is the final budgeted turn. Produce your final "
                        "answer now."
                    )
                else:
                    note = (
                        f"{remaining} turn{'s' if remaining != 1 else ''} "
                        "remain. Start converging now: complete the "
                        "deliverable and produce your final answer."
                    )
                messages.append({"role": "system", "content": note})

            state: dict = {"content": "", "tool_calls": None, "finish": None}
            acc: list[str] = []

            async def _consume():
                stream = await model_client.chat(messages, tools=tools, stream=True)
                async for ev in stream:
                    if cancel_ev.is_set():
                        return
                    if ev["type"] == "content":
                        acc.append(ev["text"])
                        if on_event:
                            on_event({"type": "text", "text": ev["text"]})
                    elif ev["type"] == "tool_calls":
                        state["tool_calls"] = ev["tool_calls"]
                    elif ev["type"] == "finish":
                        state["finish"] = ev.get("reason")

            # Race the model call against cancellation: a parent Stop must
            # interrupt an in-flight sub-agent model call promptly, not wait
            # for the stream to end on its own.
            consume_task = asyncio.create_task(_consume())
            cancel_task = asyncio.create_task(cancel_ev.wait())
            try:
                done, _ = await asyncio.wait(
                    {consume_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if cancel_task in done and consume_task not in done:
                    consume_task.cancel()
                    try:
                        await consume_task
                    except (asyncio.CancelledError, model_client.ModelError):
                        pass
                elif consume_task in done:
                    # Propagate a ModelError from the consumed task.
                    consume_task.result()
            finally:
                cancel_task.cancel()
                if not consume_task.done():
                    consume_task.cancel()
                    try:
                        await consume_task
                    except (asyncio.CancelledError, model_client.ModelError):
                        pass
            if cancel_ev.is_set():
                status = "cancelled"
                # Cancellation mid-stream: keep whatever the model said
                # before the interrupt — the partial turn is the record of
                # what the sub-agent was doing when it was stopped.
                partial = "".join(acc)
                if partial:
                    _record("assistant", partial, tool_calls=None,
                            note="interrupted mid-turn")
                    final_text = partial
                break

            content = "".join(acc)
            tool_calls = state["tool_calls"]
            _record("assistant", content, tool_calls=tool_calls)

            if not tool_calls:
                final_text = content
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                }
            )

            for tc in tool_calls:
                if cancel_ev.is_set():
                    status = "cancelled"
                    # The model streamed this call but the parent stopped
                    # before it executed: record the intent, not a result.
                    _record(
                        "assistant",
                        "".join(acc),
                        tool_calls=[tc],
                        note="tool call interrupted before execution",
                    )
                    final_text = "".join(acc)
                    break
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError as e:
                    result = {"error": f"Invalid JSON arguments: {e}"}
                else:
                    if on_event:
                        on_event({"type": "tool_start", "name": name, "args": args})
                    if name == "load_skill":
                        result = skill_registry.load_skill_into_messages(
                            args, loaded_skills, messages
                        )
                    else:
                        if gate is not None:
                            # Access mode applies to sub-agents too: the
                            # parent's gate decides before anything runs.
                            # None = approved (execute below); a dict is the
                            # denial/plan-block error result.
                            result = await gate(name, args, tc.get("id", ""))
                        else:
                            result = None
                        if result is None:
                            result = await _exec(name, args)
                    if on_event:
                        on_event({"type": "tool_result", "name": name, "result": result})

                result_str = json.dumps(result)
                if len(result_str) > MAX_RESULT_CHARS:
                    result_str = result_str[:MAX_RESULT_CHARS] + "…[truncated]"
                _record(
                    "tool",
                    result_str,
                    tool_call_id=tc.get("id", ""),
                    name=name,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result_str,
                    }
                )
            if status == "cancelled":
                break
        else:
            # for-else: loop exhausted the turn budget without a final answer.
            # One grace turn: force convergence with tools still available so
            # the deliverable (e.g. the file) actually gets written. Hard
            # bound: exactly one extra model call.
            if status == "completed":
                status = "max_turns"
                grace = True
                turns += 1
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Turn budget exhausted. Produce your final answer "
                            "NOW — finish/complete any pending deliverable "
                            "(e.g. write the file) with what you have. No "
                            "further exploration."
                        ),
                    }
                )
                state = {"content": "", "tool_calls": None, "finish": None}
                acc = []
                try:
                    stream = await model_client.chat(messages, tools=tools, stream=True)
                    async for ev in stream:
                        if cancel_ev.is_set():
                            break
                        if ev["type"] == "content":
                            acc.append(ev["text"])
                            if on_event:
                                on_event({"type": "text", "text": ev["text"]})
                        elif ev["type"] == "tool_calls":
                            state["tool_calls"] = ev["tool_calls"]
                        elif ev["type"] == "finish":
                            state["finish"] = ev.get("reason")
                except model_client.ModelError as e:
                    status = "error"
                    error = f"{type(e).__name__}: {e}"
                if cancel_ev.is_set():
                    status = "cancelled"
                content = "".join(acc)
                tool_calls = state["tool_calls"]
                _record("assistant", content, tool_calls=tool_calls,
                        note="grace turn (budget exhausted)")
                if tool_calls:
                    # Execute the grace turn's tool calls, then stop
                    # regardless — the grace turn is the last one.
                    messages.append(
                        {
                            "role": "assistant",
                            "content": content,
                            "tool_calls": tool_calls,
                        }
                    )
                    for tc in tool_calls:
                        name = tc["function"]["name"]
                        try:
                            args = json.loads(tc["function"]["arguments"] or "{}")
                        except json.JSONDecodeError as e:
                            result = {"error": f"Invalid JSON arguments: {e}"}
                        else:
                            if on_event:
                                on_event({"type": "tool_start", "name": name, "args": args})
                            if name == "load_skill":
                                result = skill_registry.load_skill_into_messages(
                                    args, loaded_skills, messages
                                )
                            else:
                                if gate is not None:
                                    result = await gate(name, args, tc.get("id", ""))
                                else:
                                    result = None
                                if result is None:
                                    result = await _exec(name, args)
                            if on_event:
                                on_event({"type": "tool_result", "name": name, "result": result})
                        result_str = json.dumps(result)
                        if len(result_str) > MAX_RESULT_CHARS:
                            result_str = result_str[:MAX_RESULT_CHARS] + "…[truncated]"
                        _record(
                            "tool",
                            result_str,
                            tool_call_id=tc.get("id", ""),
                            name=name,
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.get("id", ""),
                                "content": result_str,
                            }
                        )
                    status = "max_turns"
                    final_text = content or (
                        "[sub-agent hit its turn budget; grace turn ended on "
                        "tool calls without a final answer]"
                    )
                    grace_note = (
                        "hit turn budget; grace turn ended on tool calls "
                        "without a final answer"
                    )
                else:
                    if content:
                        grace_note = (
                            "hit turn budget; produced output in a final "
                            "wrap-up turn"
                        )
                    else:
                        grace_note = "hit turn budget without a final answer"
                    final_text = content or (
                        "[sub-agent hit its turn budget without a final answer]"
                    )
    except Exception as e:  # noqa: BLE001 — a sub-agent must never kill the parent
        status = "error"
        error = f"{type(e).__name__}: {e}"

    # Clip the transcript snapshot so one runaway run cannot bloat the DB.
    snap = json.dumps(transcript)
    if len(snap) > MAX_TRANSCRIPT_CHARS:
        snap = json.dumps(
            {
                "note": "transcript too large to store",
                "head": snap[:MAX_TRANSCRIPT_CHARS],
            }
        )
        transcript = [{"note": "transcript too large to store"}]

    # Issue #58: the sub-agent's worktree never merges itself — the branch
    # goes on the first line of the final message (clipped results must
    # still carry it), uncommitted work is salvaged, the directory is
    # removed, the branch is kept for the parent to merge.
    result: dict = {
        "agent_type": defn.name,
        "status": status,
        "turns": turns,
        "output": final_text or (f"error: {error}" if error else ""),
        **({"error": error} if error else {}),
        **({"note": grace_note} if grace_note else {}),
        "transcript": transcript,
    }
    if _used_worktree is not None:
        try:
            result = await worktrees.finalize_sub_agent(_used_worktree, result)
        except Exception:  # noqa: BLE001 — reporting must not kill the parent
            result["worktree_note"] = "worktree finalization failed; branch kept"
    elif _isolation_note:
        result["worktree_note"] = _isolation_note
    return result


# ------------------------------------------------------------------ batch


async def spawn_batch(
    calls: list[dict],
    workspace: str,
    cancel_ev: asyncio.Event,
    on_event=None,
    gate=None,
) -> dict[str, dict]:
    """Run every spawn_agent call in one parent turn in parallel (capped
    by MAX_CONCURRENT via a semaphore). Returns {call_id: result}.

    Each call: {call_id, agent_type, prompt}. Never raises per call — a
    bad agent_type or prompt returns a structured error result so the
    parent's turn survives. `gate` threads the access-mode gate (see
    run_sub_agent) into every sub-agent of the batch.
    """
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def _one(call: dict) -> tuple[str, dict]:
        call_id = call["call_id"]
        async with sem:
            defn = get_agent_def(call.get("agent_type") or "")
            if defn is None:
                available = ", ".join(d["name"] for d in list_agents())
                return call_id, {
                    "error": f"Unknown agent_type: {call.get('agent_type')}",
                    "available": available,
                }
            prompt = str(call.get("prompt") or "").strip()
            if not prompt:
                return call_id, {"error": "spawn_agent requires a prompt"}

            # Namespace this agent's gate keys by the spawn call id: two
            # parallel sub-agents (or the parent) can emit the same tool
            # call id, and the pending-answer map would collide.
            agent_gate = (
                (lambda n, a, cid: gate(n, a, f"{call_id}:{cid}")) if gate else None
            )

            agent_id = call.get("agent_id")
            if on_event:
                on_event(
                    {
                        "type": "sub_agent_spawned",
                        "agent_id": agent_id,
                        "call_id": call_id,
                        "agent_type": defn.name,
                        "prompt": prompt,
                    }
                )

            def _forward(ev: dict):
                # Wrap the inner event (text | tool_start | tool_result) as
                # sub_agent_progress. The inner "type" cannot ride through
                # the spread (the wrapper's type wins), so it is preserved
                # as "kind" — the frontend routes on it.
                inner = dict(ev)
                kind = inner.pop("type", None)
                on_event(
                    {
                        "agent_id": agent_id,
                        "call_id": call_id,
                        **inner,
                        "kind": kind,
                        "type": "sub_agent_progress",
                    }
                )

            result = await run_sub_agent(
                defn,
                prompt,
                workspace,
                cancel_ev=cancel_ev,
                on_event=_forward if on_event else None,
                gate=agent_gate,
                run_label=call_id,
            )
            if on_event:
                on_event(
                    {
                        "type": "sub_agent_done",
                        "agent_id": agent_id,
                        "call_id": call_id,
                        "agent_type": defn.name,
                        "status": result["status"],
                        "turns": result["turns"],
                    }
                )
            return call_id, result

    results = await asyncio.gather(*(_one(c) for c in calls))
    return dict(results)
