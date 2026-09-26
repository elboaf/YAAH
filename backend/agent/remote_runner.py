"""Owner-qualified remote turn runner (Phase 6).

Runs the LOCAL model/provider for a conversation owned by a remote device,
persisting the transcript through the owner's lease-checked snapshot
commit instead of the local integer-keyed conversation tables.

Identity and invariants (docs/remote-device-workspaces-and-chats.md):

- Conversation identity is ``(owner device ID, conversation ID)``; a remote
  integer ID never reaches the local agent loop's run state.
- The owner's edit lease is acquired BEFORE any model call, and workspace
  tools dispatch through the device owning the selected workspace
  (``remote:<host-id>:<path>``), never through a process-wide active host.
- Transcript persistence is in-memory during the turn; one durable
  ``queue_remote_commit`` + idempotent owner commit happens at the end.
  A lost network outcome stays durable as a pending commit and is retried
  idempotently by the existing sync-pending path.
- Failure is fail-closed: no owner session, no lease, or a lease conflict
  means the turn does not start and nothing is written.

Same-chat exclusion is two-sided: this runner holds the OWNER's lease for
its duration, and the host's local agent loop refuses to run while that
lease is active (``assert_no_active_remote_edit_lease``), so a local turn
on the host and this remote turn cannot interleave. Multiple different
chats — on this client, on the owner, or on any other device — are never
serialized: the claim is keyed by the exact owner/chat pair.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from backend.agent import remote as remote_mod
from backend.agent import remote_turn
from backend.agent.remote_run_state import remote_runs
from backend.agent.remote_turn import RemoteTurn, RemoteTurnError
from backend.db.database import (
    acknowledge_remote_commit,
    queue_remote_commit,
)

# Local-only tools that must never be offered on a remote-owned turn: they
# act on THIS machine or this process's local conversation state, which is
# not where this conversation's workspace lives.
_LOCAL_ONLY_TOOL_NAMES = {
    "screenshot", "list_windows", "focus_window", "read_ui_tree",
    "mouse_move", "mouse_click", "mouse_drag", "mouse_scroll",
    "type_text", "press_key", "wait",
    "sandbox_test", "sandbox_run", "sandbox_status", "sandbox_stop",
    # Local conversation search: the model context here is the fetched
    # owner snapshot, not the local numeric-keyed transcript tables.
    "search_conversation_history",
    # Sub-agent delegation is out of scope for Phase 6: sub-agent
    # transcripts persist through local integer-keyed tables.
    "spawn_agent",
}

_LEASE_RENEW_SECONDS = 45  # owner leases expire at 120s; renew well before


def _ndjson(event: dict) -> str:
    return json.dumps(event) + "\n"


def _message_id(index: int) -> str:
    """Stable, collision-free transcript identity for in-memory messages.

    Host snapshots already carry integer message IDs; messages this turn
    appends get local-id-prefixed strings so a snapshot commit's
    host-side normalization (integer-only IDs) allocates fresh host IDs
    for them instead of colliding with another conversation's rows.
    """
    return f"turn-{index}"


async def _local_dispatch(name: str, arguments: dict, workspace: str) -> dict:
    """Client-local tool execution for non-workspace tools."""
    from backend.agent.tools import execute_tool

    return await execute_tool(name, arguments, workspace)


def _schemas_for(workspace: str) -> list:
    from backend.agent.tools import get_schemas

    schemas = get_schemas(workspace=workspace)
    return [
        schema for schema in schemas
        if schema["function"]["name"] not in _LOCAL_ONLY_TOOL_NAMES
    ]


def _system_prompt(workspace: str, host: remote_mod.RemoteSession | None) -> str:
    from backend.agent.loop import _default_system_prompt

    # The loop's prompt builder already resolves the runtime-environment
    # line and tool list from the workspace's owning device.
    prompt = _default_system_prompt(workspace)
    if host is None and remote_mod.parse_ns(workspace) is not None:
        prompt += (
            "\n\nNote: the workspace's owning device is offline right now; "
            "workspace tools will fail until it reconnects."
        )
    return prompt


def _history_from_snapshot(messages: list[dict]) -> list[dict]:
    """Replay the fetched owner transcript into OpenAI-format model context.

    Mirrors loop.load_history's shape rules (orphaned tool rows dropped,
    image-bearing rows as parts lists) without touching the local DB.
    """
    out: list[dict] = []
    valid_call_ids: set[str] = set()
    answered_call_ids: set[str] = set()
    for row in messages:
        role = row.get("role")
        if role == "user":
            content = row.get("content") or ""
            images = row.get("images")
            if images:
                content = [
                    {"type": "text", "text": content},
                    *[
                        {"type": "image_url", "image_url": {"url": image}}
                        for image in images
                        if isinstance(image, str)
                    ],
                ]
            out.append({"role": "user", "content": content})
        elif role == "assistant":
            m = {"role": "assistant", "content": row.get("content") or ""}
            tcs = row.get("tool_calls")
            if (
                isinstance(tcs, list)
                and tcs
                and isinstance(tcs[0], dict)
                and tcs[0].get("id")
                and tcs[0].get("function")
            ):
                m["tool_calls"] = tcs
                valid_call_ids.update(
                    tc.get("id", "") for tc in tcs if tc.get("id")
                )
            out.append(m)
        elif role == "tool":
            tc_id = row.get("tool_call_id")
            if not tc_id:
                meta = (row.get("tool_calls") or [{}])[0]
                tc_id = meta.get("id", "") if isinstance(meta, dict) else ""
            if tc_id and tc_id not in valid_call_ids:
                continue  # orphaned tool result; skip to keep history valid
            answered_call_ids.add(tc_id)
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id or "",
                    "content": row.get("content") or "",
                }
            )
    return out


class _InMemoryTranscript:
    """The turn's mutable copy of the owner snapshot + this turn's output."""

    def __init__(self, conversation: dict, messages: list[dict]) -> None:
        self.conversation = dict(conversation)
        self.messages = [
            dict(message) if isinstance(message, dict) else message
            for message in messages
        ]
        self.next_index = len(self.messages)

    def append(self, role: str, content, **extra) -> dict:
        message = {"id": _message_id(self.next_index), "role": role,
                   "content": content, **extra}
        self.next_index += 1
        self.messages.append(message)
        return message


async def _commit_via_owner(
    turn: RemoteTurn,
    transcript: _InMemoryTranscript,
    holder_host_id: str,
) -> dict:
    """Durable-then-network commit of the full snapshot to the owner."""
    owner_id = turn.identity.owner_id
    conversation_id = turn.identity.conversation_id
    commit_id = await queue_remote_commit(
        owner_id, conversation_id, turn.revision,
        transcript.conversation, transcript.messages,
    )
    try:
        result = await turn.commit()
    except RemoteTurnError:
        # The durable intent stays queued with this exact commit ID; the
        # existing sync-pending path replays it idempotently on reconnect.
        raise
    await acknowledge_remote_commit(
        owner_id, conversation_id, commit_id, str(result.get("revision") or "")
    )
    return result


async def run_remote_turn(
    owner_id: str,
    conversation_id: str,
    user_text: str,
    workspace: str = "",
    model_override: str = "",
    effort_override: str | None = None,
) -> AsyncIterator[str]:
    """One owner-qualified turn. Yields the same JSON-line event shapes as
    the local loop so the existing frontend stream consumer works.

    Claims the (owner, chat) run slot for the whole turn; the owner's edit
    lease is held from before the first model call until the commit lands.
    """
    workspace = workspace or ""
    if not workspace or remote_mod.parse_ns(workspace) is None:
        # Fail closed: a remote-owned turn needs an explicit workspace owner
        # for tool dispatch; guessing the legacy active host is forbidden.
        yield _ndjson({
            "type": "error",
            "message": "remote turns require a remote:<host-id>:<path> workspace",
        })
        return

    if not await remote_runs.claim(owner_id, conversation_id):
        yield _ndjson({
            "type": "error",
            "message": "a turn is already running in this conversation",
        })
        return
    try:
        async for event in _run_claimed(
            owner_id, conversation_id, user_text, workspace,
            model_override, effort_override,
        ):
            yield event
    finally:
        await remote_runs.release(owner_id, conversation_id)


async def _run_claimed(
    owner_id: str,
    conversation_id: str,
    user_text: str,
    workspace: str,
    model_override: str,
    effort_override: str | None,
) -> AsyncIterator[str]:
    from backend.agent import model_client

    cancel_event = await remote_runs.cancel_event(owner_id, conversation_id)

    # Snapshot + lease from the explicit owner. Nothing has been written
    # yet, so any conflict here is a clean refusal.
    try:
        turn = await remote_turn.begin_remote_turn(
            owner_id, conversation_id,
            workspace=workspace, holder_id=f"remote-turn:{remote_mod.INSTANCE_ID}",
        )
    except RemoteTurnError as exc:
        yield _ndjson({
            "type": "error",
            "message": f"cannot start remote turn: {exc}",
        })
        return

    transcript = _InMemoryTranscript(turn.conversation, turn.messages)
    workspace_host = remote_mod.get_remote(
        remote_mod.parse_ns(workspace)[0]
    )

    try:
        # Persist the user message in memory only; the commit carries it.
        transcript.append("user", user_text)
        yield _ndjson({"type": "remote_turn_started", "owner_id": owner_id,
                       "conversation_id": conversation_id})

        messages = [
            {"role": "system", "content": _system_prompt(workspace, workspace_host)}
        ]
        messages.extend(_history_from_snapshot(transcript.messages))

        tools = _schemas_for(workspace)
        last_renew = asyncio.get_event_loop().time()

        step = 0
        while True:
            if cancel_event is not None and cancel_event.is_set():
                # Cancelled before any model output: nothing to commit.
                yield _ndjson({"type": "stopped", "reason": "cancelled by user"})
                return

            state: dict = {"content": "", "tool_calls": None, "finish": None,
                           "usage": None}
            async def _consume() -> None:
                stream = await model_client.chat(
                    messages, tools=tools, stream=True,
                    model=model_override, effort=effort_override,
                )
                async for ev in stream:
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    kind = ev["type"]
                    if kind == "content":
                        state["content"] += ev.get("text", "")
                        yield _ndjson({"type": "text", "text": ev.get("text", "")})
                    elif kind == "thinking":
                        yield _ndjson({"type": "thinking", "text": ev.get("text", "")})
                    elif kind == "model_call":
                        yield _ndjson({"type": "model_call", **{
                            k: ev.get(k, "") for k in ("provider", "model")}})
                    elif kind == "tool_calls":
                        state["tool_calls"] = ev["tool_calls"]
                    elif kind == "finish":
                        state["finish"] = ev.get("reason")
                    elif kind == "usage":
                        state["usage"] = ev.get("usage")

            # Lease renewal heartbeat: fail the turn if the owner lease is
            # lost mid-run (expired, revoked, or the owner went offline).
            now = asyncio.get_event_loop().time()
            if now - last_renew >= _LEASE_RENEW_SECONDS:
                try:
                    await turn.renew_lease()
                except RemoteTurnError as exc:
                    yield _ndjson({
                        "type": "error",
                        "message": f"remote edit lease lost: {exc}",
                    })
                    return
                last_renew = now

            try:
                async for line in _consume():
                    yield line
            except Exception as exc:  # ModelError and transport failures
                yield _ndjson({"type": "error",
                               "message": f"{type(exc).__name__}: {exc}"})
                return

            if cancel_event is not None and cancel_event.is_set():
                # Cancelled mid-emission: keep what arrived as the transcript
                # (durable pending commit), then stop.
                yield _ndjson({"type": "stopped", "reason": "cancelled by user"})
                break

            assistant_content = state["content"]
            tool_calls = state["tool_calls"]
            transcript.append("assistant", assistant_content,
                              tool_calls=tool_calls)
            messages.append({"role": "assistant",
                             "content": assistant_content,
                             **({"tool_calls": tool_calls} if tool_calls else {})})

            if not tool_calls:
                break  # final answer

            step += 1
            from backend.agent.config import load_config

            try:
                max_steps = int(load_config().get("max_steps") or 0)
            except (TypeError, ValueError):
                max_steps = 0
            if max_steps and step >= max_steps:
                yield _ndjson({"type": "error",
                               "message": "step budget exhausted"})
                break

            for tc in tool_calls:
                if cancel_event is not None and cancel_event.is_set():
                    yield _ndjson({"type": "stopped",
                                   "reason": "cancelled by user"})
                    break
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError as e:
                    args = None
                    result = {"error": f"Invalid JSON arguments: {e}"}
                if args is not None:
                    yield _ndjson({"type": "tool_start", "name": name,
                                   "args": args, "call_id": tc.get("id", "")})
                    try:
                        result = await turn.dispatch_tool(
                            name, args, _local_dispatch,
                        )
                    except RemoteTurnError as exc:
                        result = {"error": str(exc)}
                    if name in remote_mod.REMOTE_TOOLS and isinstance(result, dict):
                        # Shell tools return no chunk stream remotely; emit
                        # the final result exactly like a completed local run.
                        pass
                yield _ndjson({"type": "tool_result", "name": name,
                               "result": result, "call_id": tc.get("id", "")})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": json.dumps(result),
                })
                transcript.append("tool", json.dumps(result),
                                  tool_calls=[{"id": tc.get("id", ""),
                                               "name": name}],
                                  tool_call_id=tc.get("id", ""))
            else:
                continue
            break  # cancel inside the tool loop

        # Turn body complete (final answer or cancel): commit the snapshot.
        try:
            result = await _commit_via_owner(turn, transcript, owner_id)
        except RemoteTurnError as exc:
            # The pending commit stays durable; the transcript update reaches
            # the owner on the next sync-pending replay.
            yield _ndjson({
                "type": "remote_commit_pending",
                "message": f"commit deferred: {exc}",
            })
            yield _ndjson({"type": "done"})
            return
        yield _ndjson({"type": "usage",
                       "usage_tokens": (state.get("usage") or {}).get("prompt_tokens")})
        yield _ndjson({"type": "remote_turn_committed",
                       "revision": result.get("revision", "")})
        yield _ndjson({"type": "done"})
    finally:
        # Release the owner lease; a lost release acknowledgement is safe —
        # the host's expiry window recovers it.
        try:
            await turn.release()
        except RemoteTurnError:
            pass


# Re-exported for the API layer's claim/cancel wiring.
remote_runs_cancel = remote_runs.cancel
remote_runs_is_running = remote_runs.is_running
