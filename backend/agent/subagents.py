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
from backend.agent.tools import (
    MEMORY_TOOLS,
    execute_tool,
    get_schemas,
    memory_workspace_for,
    workspace_root,
)

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
# block on the user) or spawn_agent (no nesting).
_ALWAYS_EXCLUDED = {
    "ask_user", "spawn_agent", "search_conversation_history",
}

_EXPLORE_TOOLS = {
    "read_file", "search_files", "web_search", "web_fetch", "view_image",
    "load_skill",
}


def _exclusion_clause() -> str:
    """The index description is generated from the SAME sets the
    runtime enforces (#189/SYN-23), so prose and enforcement cannot
    drift apart again."""
    excluded = sorted(_ALWAYS_EXCLUDED)
    return "all tools except: " + ", ".join(excluded)


def _builtin_general_purpose() -> AgentDef:
    return AgentDef(
        name="general-purpose",
        description=(
            "The default sub-agent for broad tasks: implement a small "
            "feature, fix a clear issue, run verification commands, or "
            "carry a self-contained piece of work forward in isolation. "
            "Has access to " + _exclusion_clause() + "."
        ),
        body=(
            "You are a focused sub-agent working inside a larger task. "
            "Complete the delegated work independently: explore what you "
            "need, make the changes, verify them, and report a concise "
            "final summary. When a fact about external software matters "
            "(library/API behavior, error messages, config formats, "
            "versions), prefer a quick web_search over inferring it from "
            "code alone; private code, local state, and secrets are not "
            "on the internet."
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
            "not modify anything. For questions about external software "
            "(library/API behavior, error messages, config formats, "
            "versions), search the web (web_search/web_fetch) before "
            "concluding from code alone; private code, local state, and "
            "secrets are not on the internet."
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
    from backend.agent.tools import DELEGATION_POLICY
    lines.append(
        "Delegate proactively: when a chunk of work is self-contained — "
        "a sweep over many files, independent research threads, broad "
        "exploration that would eat this conversation's context — prefer "
        "spawning a sub-agent over doing it inline. "
        # #181: the policy core is the single DELEGATION_POLICY constant,
        # shared with the spawn_agent schema and help docs.
        + DELEGATION_POLICY
        + " Announce each delegation in one line before the calls, and "
        "summarize what came back when the results arrive. Write each "
        "sub-agent prompt fully self-contained (it sees nothing else "
        "from this conversation — include paths, constraints, and "
        "exactly what to report back), and don't fragment one small "
        "edit into delegation."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ runner


def _resolve_tools(defn: AgentDef, workspace: str | None = None) -> list[dict]:
    """Tool schemas for a sub-agent: built-ins minus always-excluded and
    computer-use, then the definition's allow/deny lists applied.

    #188: the workspace is threaded through so schemas resolve on the
    workspace's OWN host (remote namespaced -> that host; otherwise the
    legacy active host / local machine) — the same resolution execute_tool
    uses, so the schemas and the prompt's env line can never describe two
    different hosts."""
    schemas = get_schemas(workspace=workspace)
    allowed: dict[str, dict] = {}
    for s in schemas:
        n = s["function"]["name"]
        if n in _ALWAYS_EXCLUDED:
            continue
        allowed[n] = s
    if defn.tools is not None:
        allowed = {n: s for n, s in allowed.items() if n in set(defn.tools)}
    for n in defn.disallowed_tools:
        allowed.pop(n, None)
    return list(allowed.values())


def _sub_agent_system_prompt(
    defn: AgentDef,
    workspace: str,
    workspace_notes: str = "",
    memory_workspace: str | None = None,
) -> str:
    """System prompt for a sub-agent run: its definition body plus the
    same environment grounding the parent gets (env line, workspace
    notes, skills index) so commands and paths are valid for the host.
    memory_workspace (#346): the parent turn's canonical memory key —
    the pre-rebind workspace root — so the injected index reads the
    same store the sub's memory tool calls resolve to. None falls back
    to `workspace` (direct renders, tests)."""
    from backend.agent import remote as remote_mod
    from backend.agent.loop import (
        _agents_notes,
        _local_env_line,
    )

    # #188: ONE host resolution for the whole prompt — the env line, the
    # prose tool list, and (via run_sub_agent) the schemas all derive from
    # the workspace's owning host, never from the legacy active host or
    # the client's os.name.
    host = remote_mod.remote_for_workspace(workspace)
    if host is None and remote_mod.parse_ns(workspace) is None:
        # Local workspace: keep the legacy fallback to the active host
        # (its tools execute there when connected).
        host = remote_mod.get_remote()
    if host is not None:
        # Tools forward to the host, so the sub-agent needs the HOST's
        # environment grounding (OS/shell/workspace), not this machine's.
        env = host.env_line(workspace)
    else:
        env = _local_env_line()
    # #181: derive the prose from the SAME computation as _resolve_tools,
    # so the prompt can never name (or omit) a tool the schemas disagree
    # with. The only prose-only names are the shell line's annotations.
    from backend.agent.tools import tool_prose_list

    resolved = _resolve_tools(defn, workspace=workspace)
    tools_ann = {"bash": "shell commands"}
    if host is not None and host.windows:
        tools_ann["powershell"] = "Windows PowerShell"
    elif host is None and os.name == "nt":
        tools_ann["powershell"] = "Windows PowerShell"
    tools_line = tool_prose_list(resolved, tools_ann)
    prompt = (
        f"You are a sub-agent (agent_type: {defn.name}) spawned by a "
        f"parent agent. {defn.body}\n\n"
        f"{env}\n\n"
        f"{tools_line}\n\n"
        "Guidelines:\n"
        "- You cannot ask the user questions: decide for yourself, act on "
        "the most reasonable interpretation, and report the assumption in "
        "your final message.\n"
        "- You cannot spawn sub-agents of your own.\n"
        "- Your final message is the only thing the parent receives. Make "
        "it self-contained: what you did, what you changed (paths), what "
        "you verified, and any assumptions or follow-ups.\n"
        "- The user sees your streamed text live in the parent's transcript. "
        "Lead with a one-line summary of what you're doing, then work.\n"
        "- Paths are relative to the workspace root.\n"
        "- Git work goes through the bash tool (git is on PATH); never "
        "open git's interactive editor - pass -m to commit.\n"
    )
    # #334: the notes arrive from the parent (which fetched them already —
    # remotely through the channel); the sync fallback covers direct calls.
    notes = workspace_notes if workspace_notes else _agents_notes(workspace)
    if notes:
        prompt += f"\n\n---\n\n{notes}"
    skill_index = skill_registry.index_for_prompt()
    if skill_index:
        prompt += f"\n\n---\n\n{skill_index}"
    # #182: sub-agents resolving the memory tools get the same
    # persistent-memory block the parent gets, so the tool description's
    # claim ("the index of saved memories is in your system prompt every
    # turn") is true in every context where the tool is offered. The
    # index is project-keyed to the workspace the sub-agent is spawned
    # into and empty for a fresh project (matching the parent).
    from backend.agent import memory as memory_mod

    resolved_names = {s["function"]["name"] for s in resolved}
    if set(MEMORY_TOOLS) & resolved_names:
        try:
            # #346: key the index to the canonical root the parent
            # resolved (pre-rebind), matching what the sub's memory
            # tool calls resolve to through the funnel injection.
            memory_block = memory_mod.index_for_prompt(
                memory_workspace or workspace
            )
        except Exception:  # noqa: BLE001 — optional context never breaks a turn
            memory_block = ""
        if memory_block:
            prompt += f"\n\n---\n\n{memory_block}"
    return prompt


async def run_sub_agent(
    defn: AgentDef,
    prompt: str,
    workspace: str,
    cancel_ev: asyncio.Event | None = None,
    on_event=None,
    gate=None,
    run_label: str = "",
    conversation_id: int | None = None,
    workspace_notes: str = "",
    memory_workspace: str | None = None,
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
    # The sub-agent starts on the workspace it was handed.
    run_workspace = str(workspace)

    # #346: the sub's memory calls resolve to the parent's canonical
    # root (handed down pre-rebind) when present; funnel injection from
    # the run workspace covers every other shape.
    sub_memory_workspace = memory_workspace or memory_workspace_for(run_workspace)

    async def _exec(name: str, args: dict, tool_call_id: str = "") -> dict:
        def on_chunk(chunk: str) -> None:
            if on_event and chunk:
                on_event({"type": "tool_progress", "tool_call_id": tool_call_id, "chunk": chunk})

        async def execute(name: str, args: dict, path: str) -> dict:
            return await execute_tool(
                name, args, path,
                on_chunk=on_chunk if on_event else None,
                # #303: chat-scoped tools (search_conversation_history)
                # need the calling chat's id. branch_select left the
                # chat-scoped set with the direct world (#361).
                conversation_id=conversation_id,
                # #346: memory calls resolve to the parent turn's
                # canonical root (handed down pre-rebind); the funnel
                # ignores the kwarg for every other tool.
                memory_workspace=sub_memory_workspace,
            )
        result = await execute(name, args, run_workspace)
        return result

    messages = [
        {
            "role": "system",
            "content": _sub_agent_system_prompt(
                defn, workspace,
                workspace_notes=workspace_notes,
                memory_workspace=memory_workspace,
            ),
        },
        {"role": "user", "content": prompt},
    ]
    tools = _resolve_tools(defn, workspace=run_workspace)
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
        # #190: closing turns carry at most one convergence nudge — a new
        # nudge replaces the previous one in place instead of accumulating.
        nudge_idx: int | None = None
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
                    # #190: this turn is NOT final — the for-else below
                    # grants one grace wrap-up turn when it ends on tool
                    # calls, so the text must not claim otherwise.
                    note = (
                        "The turn budget ends after this turn; one wrap-up "
                        "turn may follow. Produce your final answer now."
                    )
                else:
                    note = (
                        f"{remaining} turn{'s' if remaining != 1 else ''} "
                        "remain. Start converging now: complete the "
                        "deliverable and produce your final answer."
                    )
                if nudge_idx is not None:
                    messages[nudge_idx] = {"role": "system", "content": note}
                else:
                    messages.append({"role": "system", "content": note})
                    nudge_idx = len(messages) - 1

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
                    elif ev["type"] == "thinking":
                        # UI-only telemetry; never record reasoning in the transcript.
                        if on_event:
                            on_event({"type": "thinking", "text": ev.get("text", "")})
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
                args = {}
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError as e:
                    result = {"error": f"Invalid JSON arguments: {e}"}
                else:
                    if on_event:
                        on_event(
                            {
                                "type": "tool_start",
                                "tool_call_id": tc.get("id", ""),
                                "name": name,
                                "args": args,
                            }
                        )
                    if name == "load_skill":
                        result = skill_registry.load_skill_into_messages(
                            args, loaded_skills, messages
                        )
                    else:
                        # #188 belt-and-braces: the allowlist is enforced
                        # at execution time too, not only when the schemas
                        # were listed — a future regression that leaks a
                        # schema hits this structured error, not a write.
                        allowed_names = {
                            s["function"]["name"] for s in tools
                        }
                        if name not in allowed_names:
                            result = {
                                "error": (
                                    f"Tool '{name}' is not in agent "
                                    f"'{defn.name}'s allowed tool set."
                                )
                            }
                        elif gate is not None:
                            # Access mode applies to sub-agents too: the
                            # parent's gate decides before anything runs.
                            # None = approved (execute below); a dict is the
                            # denial/plan-block error result.
                            result = await gate(name, args, tc.get("id", ""))
                        else:
                            result = None
                        if result is None:
                            result = await _exec(name, args, tc.get("id", ""))
                    if on_event:
                        on_event(
                            {
                                "type": "tool_result",
                                "tool_call_id": tc.get("id", ""),
                                "name": name,
                                "result": result,
                            }
                        )

                result_str = json.dumps(result)
                if len(result_str) > MAX_RESULT_CHARS:
                    result_str = result_str[:MAX_RESULT_CHARS] + "…[truncated]"
                _record(
                    "tool",
                    result_str,
                    tool_call_id=tc.get("id", ""),
                    name=name,
                    args=args,
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
                        elif ev["type"] == "thinking":
                            # UI-only telemetry; never record reasoning in the transcript.
                            if on_event:
                                on_event({"type": "thinking", "text": ev.get("text", "")})
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
                        args = {}
                        try:
                            args = json.loads(tc["function"]["arguments"] or "{}")
                        except json.JSONDecodeError as e:
                            result = {"error": f"Invalid JSON arguments: {e}"}
                        else:
                            if on_event:
                                on_event(
                                    {
                                        "type": "tool_start",
                                        "tool_call_id": tc.get("id", ""),
                                        "name": name,
                                        "args": args,
                                    }
                                )
                            if name == "load_skill":
                                result = skill_registry.load_skill_into_messages(
                                    args, loaded_skills, messages
                                )
                            else:
                                # #188 belt-and-braces: the grace-turn
                                # loop enforces the allowlist too — a
                                # budget-exhausted turn gets no wider
                                # tool access than a normal one.
                                allowed_names = {
                                    s["function"]["name"] for s in tools
                                }
                                if name not in allowed_names:
                                    result = {
                                        "error": (
                                            f"Tool '{name}' is not in agent "
                                            f"'{defn.name}'s allowed tool set."
                                        )
                                    }
                                elif gate is not None:
                                    result = await gate(name, args, tc.get("id", ""))
                                else:
                                    result = None
                                if result is None:
                                    result = await _exec(name, args, tc.get("id", ""))
                            if on_event:
                                on_event(
                                    {
                                        "type": "tool_result",
                                        "tool_call_id": tc.get("id", ""),
                                        "name": name,
                                        "result": result,
                                    }
                                )
                        result_str = json.dumps(result)
                        if len(result_str) > MAX_RESULT_CHARS:
                            result_str = result_str[:MAX_RESULT_CHARS] + "…[truncated]"
                        _record(
                            "tool",
                            result_str,
                            tool_call_id=tc.get("id", ""),
                            name=name,
                            args=args,
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

    result: dict = {
        "agent_type": defn.name,
        "status": status,
        "turns": turns,
        "output": final_text or (f"error: {error}" if error else ""),
        **({"error": error} if error else {}),
        **({"note": grace_note} if grace_note else {}),
        "transcript": transcript,
    }
    return result


# ------------------------------------------------------------------ batch


async def spawn_batch(
    calls: list[dict],
    workspace: str,
    cancel_ev: asyncio.Event,
    on_event=None,
    gate=None,
    conversation_id: int | None = None,
    workspace_notes: str = "",
    memory_workspace: str | None = None,
) -> dict[str, dict]:
    """Run every spawn_agent call in one parent turn in parallel (capped
    by MAX_CONCURRENT via a semaphore). Returns {call_id: result}.

    Each call: {call_id, agent_type, prompt}. Never raises per call — a
    bad agent_type or prompt returns a structured error result so the
    parent's turn survives. `gate` threads the access-mode gate (see
    run_sub_agent) into every sub-agent of the batch.
    `memory_workspace` (#346) threads the parent turn's canonical memory
    key (its pre-rebind workspace root) into every sub-agent, so a
    sub's save/read resolves the same store the parent's prompt block
    injected.
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
                # Wrap inner events as sub_agent_progress. call_id always
                # identifies the parent spawn; tool_call_id stays distinct so
                # nested output and results route within this run.
                inner = dict(ev)
                kind = inner.pop("type", None)
                inner_call_id = inner.pop("call_id", None)
                tool_call_id = inner.pop("tool_call_id", None)
                if kind in {"tool_start", "tool_progress", "tool_result"}:
                    tool_call_id = tool_call_id or inner_call_id
                on_event(
                    {
                        "agent_id": agent_id,
                        **inner,
                        "call_id": call_id,
                        **({"tool_call_id": tool_call_id} if tool_call_id else {}),
                        **(
                            {"approval_call_id": inner_call_id}
                            if kind in {"approval_request", "approval_decision"} and inner_call_id
                            else {}
                        ),
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
                # #303: chat-scoped tools resolve to the PARENT chat.
                conversation_id=conversation_id,
                workspace_notes=workspace_notes,
                # #346: memory resolves to the parent's canonical root.
                memory_workspace=memory_workspace,
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
                        **({"note": result["note"]} if result.get("note") else {}),
                    }
                )
            return call_id, result

    results = await asyncio.gather(*(_one(c) for c in calls))
    return dict(results)
