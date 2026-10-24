"""Owner-qualified run claims for remote-owned conversations.

This registry is deliberately separate from the local integer-keyed agent loop.
It provides identity-safe claim/query/release semantics only; it does not start
agent work, persist transcripts, or route tools.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class _RunState:
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


class RemoteRunRegistry:
    """In-process run state keyed by stable owner ID and opaque chat ID.

    Owner and conversation IDs are strings to preserve their exact wire identity.
    A given owner/chat pair may have at most one active run. Different owners
    may independently run chats that happen to have the same conversation ID.
    """

    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], _RunState] = {}
        self._lock = asyncio.Lock()

    async def claim(self, owner_id: str, conversation_id: str) -> bool:
        """Claim one owner-qualified chat; return false if already running."""
        key = self._key(owner_id, conversation_id)
        async with self._lock:
            if key in self._runs:
                return False
            self._runs[key] = _RunState()
            return True

    async def is_running(self, owner_id: str, conversation_id: str) -> bool:
        """Return whether this exact owner/chat pair has an active run."""
        key = self._key(owner_id, conversation_id)
        async with self._lock:
            return key in self._runs

    async def running_convs(self, owner_id: str) -> list[str]:
        """Return the conversation IDs this owner has mid-turn.

        The sidebar's run-state parity (issue #457) needs a coarse truth the
        local stream cannot provide: a turn started on another window, after an
        app restart, or with the transcript dialog closed still has to show the
        yellow tracer on the device's rows. One scan answers every chat on the
        device, keyed per conversation so a finished bar can be derived on the
        frontend by the running->not-running transition on that exact row.
        """
        async with self._lock:
            return [cid for (own, cid) in self._runs if own == owner_id]

    async def cancel_event(self, owner_id: str, conversation_id: str) -> asyncio.Event | None:
        """Return this run's cancellation signal, if the run is active."""
        key = self._key(owner_id, conversation_id)
        async with self._lock:
            state = self._runs.get(key)
            return state.cancel_event if state else None

    async def cancel(self, owner_id: str, conversation_id: str) -> bool:
        """Signal cancellation for exactly one owner/chat pair."""
        event = await self.cancel_event(owner_id, conversation_id)
        if event is None:
            return False
        event.set()
        return True

    async def release(self, owner_id: str, conversation_id: str) -> bool:
        """Release a claim; return false if it was not active."""
        key = self._key(owner_id, conversation_id)
        async with self._lock:
            return self._runs.pop(key, None) is not None

    @staticmethod
    def _key(owner_id: str, conversation_id: str) -> tuple[str, str]:
        if not owner_id or not conversation_id:
            raise ValueError("owner_id and conversation_id must be non-empty")
        return owner_id, conversation_id


remote_runs = RemoteRunRegistry()
