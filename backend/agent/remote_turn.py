"""Isolated owner-aware remote turn prototype.

This module is intentionally not connected to ``loop.py`` or ``main.py``. It
provides the Phase 6 seam only: a remote conversation owner is resolved
explicitly, a fresh host snapshot and edit lease are required before work, the
transcript is changed in memory, and a full snapshot is committed through the
existing host endpoints. Workspace ownership is independent from conversation
ownership.
"""

from __future__ import annotations

import copy
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from backend.agent import remote as remote_mod


class RemoteTurnError(RuntimeError):
    """Base error for a turn that cannot safely proceed or persist."""


class RemoteTurnUnavailable(RemoteTurnError):
    """The explicit owner is unknown, offline, or returned an invalid response."""


class RemoteTurnConflict(RemoteTurnError):
    """The host rejected a lease or snapshot because of a conflict."""


@dataclass(frozen=True)
class RemoteTurnIdentity:
    """Stable ownership context for one turn (workspace is not conversation owner)."""

    owner_id: str
    conversation_id: str
    workspace: str
    workspace_owner_id: str


SessionResolver = Callable[[str], Any | None]
LocalToolDispatcher = Callable[[str, dict, str], Awaitable[dict]]


def _response_payload(response: Any, operation: str) -> dict:
    """Decode a host/proxy response and turn protocol failures into safe errors."""
    status = getattr(response, "status_code", None)
    try:
        payload = response.json() if callable(getattr(response, "json", None)) else response
    except Exception as exc:  # malformed/unavailable response is never a green path
        raise RemoteTurnUnavailable(f"remote {operation} returned invalid JSON") from exc

    if status is not None and not 200 <= status < 300:
        detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
        code = detail.get("code") if isinstance(detail, dict) else None
        message = str(code or detail or f"HTTP {status}")
        if status in (409, 423):
            raise RemoteTurnConflict(f"remote {operation} conflict: {message}")
        raise RemoteTurnUnavailable(f"remote {operation} failed ({status}): {message}")
    if not isinstance(payload, dict):
        raise RemoteTurnUnavailable(f"remote {operation} returned an invalid payload")
    return payload


def _require_session(resolver: SessionResolver, owner_id: str):
    try:
        session = resolver(owner_id)
    except Exception as exc:
        raise RemoteTurnUnavailable(f"could not resolve remote owner {owner_id!r}") from exc
    if session is None:
        raise RemoteTurnUnavailable(f"remote owner {owner_id!r} is not connected")
    if getattr(session, "host_id", None) != owner_id:
        raise RemoteTurnUnavailable("resolved remote session does not match the requested owner")
    return session


class RemoteTurn:
    """One lease-protected turn with an in-memory transcript snapshot."""

    def __init__(
        self,
        *,
        identity: RemoteTurnIdentity,
        session: Any,
        conversation: dict,
        messages: list[dict],
        revision: str,
        lease_token: str,
        session_resolver: SessionResolver,
    ) -> None:
        self.identity = identity
        self._session = session
        self._session_resolver = session_resolver
        self.conversation = copy.deepcopy(conversation)
        self.messages = copy.deepcopy(messages)
        self.revision = revision
        self._lease_token: str | None = lease_token
        self._commit_id: str | None = None
        self._commit_payload: dict | None = None
        self._closed = False

    @property
    def lease_token(self) -> str:
        if self._closed or not self._lease_token:
            raise RemoteTurnUnavailable("remote turn no longer holds an edit lease")
        return self._lease_token

    def append_message(self, message: dict) -> None:
        """Append a copied transcript item in memory; no host write occurs."""
        self._ensure_open()
        if not isinstance(message, dict):
            raise TypeError("message must be a dict")
        self.messages.append(copy.deepcopy(message))
        # A subsequent edit changes the request; assign a fresh idempotency key.
        self._invalidate_pending_commit()

    def update_message(self, message_id: str | int, **changes: Any) -> None:
        """Update exactly one existing message in memory, preserving its identity."""
        self._ensure_open()
        target = str(message_id)
        matches = [message for message in self.messages if str(message.get("id")) == target]
        if len(matches) != 1:
            raise KeyError(f"expected one message with id {target!r}; found {len(matches)}")
        matches[0].update(copy.deepcopy(changes))
        self._invalidate_pending_commit()

    async def renew_lease(self) -> dict:
        """Extend this turn's host lease; failure means the turn must stop."""
        self._ensure_open()
        path = self._lease_path()
        try:
            response = await self._session.proxy(
                "POST", path, json_body={"lease_token": self.lease_token},
            )
        except Exception as exc:
            raise RemoteTurnUnavailable("remote lease renewal outcome is unknown") from exc
        result = _response_payload(response, "lease renewal")
        if result.get("lease_token") != self.lease_token:
            raise RemoteTurnConflict("remote lease renewal returned a different token")
        return result

    async def commit(
        self,
        *,
        conversation: dict | None = None,
        messages: list[dict] | None = None,
        commit_id: str | None = None,
    ) -> dict:
        """Commit the full snapshot; retries after ambiguous I/O reuse its ID/payload.

        ``conversation``/``messages``/``commit_id`` let a caller (the remote
        turn runner) supply its own mutated transcript copy and a durable
        pre-assigned commit ID; without them this turn's pristine snapshot is
        committed under a fresh ID. Retries must pass the same values so the
        payload — and therefore host-side idempotency — is unchanged.
        """
        self._ensure_open()
        if (
            self._commit_payload is None
            or conversation is not None
            or messages is not None
            or commit_id is not None
        ):
            self._commit_id = commit_id or secrets.token_urlsafe(24)
            self._commit_payload = {
                "lease_token": self.lease_token,
                "revision": self.revision,
                "commit_id": self._commit_id,
                "conversation": copy.deepcopy(
                    conversation if conversation is not None else self.conversation
                ),
                "messages": copy.deepcopy(
                    messages if messages is not None else self.messages
                ),
            }
        # Use owner lookup again instead of assuming that any globally active
        # connection still belongs to this conversation.
        session = _require_session(self._session_resolver, self.identity.owner_id)
        path = self._commit_path()
        try:
            response = await session.proxy(
                "POST", path, json_body=copy.deepcopy(self._commit_payload),
            )
        except RemoteTurnError:
            raise
        except Exception as exc:
            raise RemoteTurnUnavailable(
                "remote commit outcome is unknown; retry this turn to replay its commit ID"
            ) from exc
        result = _response_payload(response, "snapshot commit")
        if result.get("commit_id") != self._commit_id or not result.get("revision"):
            raise RemoteTurnUnavailable("remote commit acknowledgement did not match the request")
        self.revision = str(result["revision"])
        return result

    async def release(self) -> bool:
        """Release only this turn's token. Lost release acknowledgements are safe to retry."""
        if self._closed:
            return False
        token = self._lease_token
        if not token:
            self._closed = True
            return False
        # Resolve the owner afresh. Never redirect a release to another session.
        session = _require_session(self._session_resolver, self.identity.owner_id)
        try:
            response = await session.proxy(
                "DELETE", self._lease_path(), json_body={"lease_token": token},
            )
        except Exception as exc:
            raise RemoteTurnUnavailable(
                "remote lease release outcome is unknown; host expiry remains the recovery path"
            ) from exc
        result = _response_payload(response, "lease release")
        if result.get("released") is not True and result.get("released") is not False:
            raise RemoteTurnUnavailable("remote lease release returned an invalid acknowledgement")
        self._closed = True
        self._lease_token = None
        return bool(result["released"])

    async def dispatch_tool(
        self,
        name: str,
        arguments: dict,
        local_dispatch: LocalToolDispatcher,
    ) -> dict:
        """Dispatch remote workspace tools to its owner; otherwise use local tools."""
        self._ensure_open()
        workspace_owner = remote_mod.parse_ns(self.identity.workspace)
        if name in remote_mod.REMOTE_TOOLS and workspace_owner is not None:
            host_id, _raw_path = workspace_owner
            if host_id != self.identity.workspace_owner_id:
                raise RemoteTurnConflict("workspace namespace no longer matches its owner")
            session = _require_session(self._session_resolver, host_id)
            try:
                return await session.exec_tool(
                    name, arguments, workspace=self.identity.workspace,
                )
            except Exception as exc:
                raise RemoteTurnUnavailable("remote workspace tool failed") from exc
        return await local_dispatch(name, arguments, self.identity.workspace)

    async def __aenter__(self) -> "RemoteTurn":
        self._ensure_open()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        try:
            await self.release()
        except RemoteTurnError:
            if exc is None:
                raise
        return False

    def _ensure_open(self) -> None:
        if self._closed or not self._lease_token:
            raise RemoteTurnUnavailable("remote turn is closed or no longer holds a lease")

    def _invalidate_pending_commit(self) -> None:
        self._commit_payload = None
        self._commit_id = None

    def _lease_path(self) -> str:
        return f"/api/remote/conversations/{quote(self.identity.conversation_id, safe='')}/lease"

    def _commit_path(self) -> str:
        return f"/api/remote/conversations/{quote(self.identity.conversation_id, safe='')}/commit"


async def begin_remote_turn(
    owner_id: str,
    conversation_id: str,
    *,
    workspace: str = "",
    workspace_owner_id: str | None = None,
    holder_id: str = "remote-turn-prototype",
    session_resolver: SessionResolver | None = None,
) -> RemoteTurn:
    """Fetch the live owner snapshot, validate identity, then acquire its lease.

    The owner is always explicit; the legacy active-remote fallback is never
    used. ``workspace_owner_id`` can be supplied to assert UI/backend ownership
    metadata, while a ``remote:<host>:<path>`` namespace independently provides
    the dispatch owner.
    """
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("owner_id must be a non-empty string")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("conversation_id must be a non-empty string")
    resolver = session_resolver or remote_mod.get_remote

    parsed_workspace = remote_mod.parse_ns(workspace)
    if parsed_workspace is not None:
        derived_workspace_owner = parsed_workspace[0]
        if not derived_workspace_owner:
            raise ValueError("remote workspace must include an owner")
        if workspace_owner_id is not None and workspace_owner_id != derived_workspace_owner:
            raise ValueError("workspace owner does not match workspace namespace")
    else:
        derived_workspace_owner = workspace_owner_id or "local"
        if derived_workspace_owner != "local":
            raise ValueError("workspace owner must match a remote:<host-id>: namespace")

    identity = RemoteTurnIdentity(
        owner_id=owner_id,
        conversation_id=conversation_id,
        workspace=workspace,
        workspace_owner_id=derived_workspace_owner,
    )
    session = _require_session(resolver, owner_id)
    snapshot_path = f"/api/remote/conversations/{quote(conversation_id, safe='')}/snapshot"
    try:
        response = await session.proxy("GET", snapshot_path)
    except Exception as exc:
        raise RemoteTurnUnavailable("remote conversation snapshot is unavailable") from exc
    snapshot = _response_payload(response, "conversation snapshot")
    conversation = snapshot.get("conversation")
    messages = snapshot.get("messages")
    revision = snapshot.get("revision")
    if (
        not isinstance(conversation, dict)
        or str(conversation.get("id", "")) != conversation_id
        or not isinstance(messages, list)
        or not isinstance(revision, str)
        or not revision
        or conversation.get("revision", revision) != revision
    ):
        raise RemoteTurnUnavailable("remote snapshot identity or shape did not match the request")

    lease_path = f"/api/remote/conversations/{quote(conversation_id, safe='')}/lease"
    try:
        lease_response = await session.proxy(
            "POST", lease_path,
            json_body={"revision": revision, "holder_id": holder_id},
        )
    except Exception as exc:
        raise RemoteTurnUnavailable("remote edit lease is unavailable") from exc
    lease = _response_payload(lease_response, "edit lease acquisition")
    token = lease.get("lease_token")
    if not isinstance(token, str) or not token or lease.get("revision", revision) != revision:
        raise RemoteTurnConflict("remote lease was not acquired for the fetched revision")

    return RemoteTurn(
        identity=identity,
        session=session,
        conversation=conversation,
        messages=messages,
        revision=revision,
        lease_token=token,
        session_resolver=resolver,
    )
