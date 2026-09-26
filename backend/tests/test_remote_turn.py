"""Owner-qualified remote turn prototype tests (not wired into the agent loop)."""

import copy

import pytest

from backend.agent.remote_turn import (
    RemoteTurnConflict,
    RemoteTurnUnavailable,
    begin_remote_turn,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, host_id, snapshot=None):
        self.host_id = host_id
        self.snapshot = copy.deepcopy(snapshot or {
            "revision": "r:4",
            "conversation": {"id": 731, "title": "Remote chat", "workspace": "C:/repo"},
            "messages": [{"id": 12, "role": "user", "content": "hello"}],
        })
        self.calls = []
        self.exec_calls = []
        self.commit_results = {}
        self.fail_next_commit = False
        self.response_overrides = {}

    async def proxy(self, method, path, *, json_body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(json_body)))
        override = self.response_overrides.get((method, path))
        if override is not None:
            return override
        if path.endswith("/snapshot") and method == "GET":
            return FakeResponse(copy.deepcopy(self.snapshot))
        if path.endswith("/lease") and method == "POST":
            if json_body.get("lease_token"):
                return FakeResponse({"ok": True, "lease_token": json_body["lease_token"], "lease_seconds": 120})
            return FakeResponse({"ok": True, "lease_token": f"lease-{self.host_id}", "revision": json_body["revision"], "lease_seconds": 120})
        if path.endswith("/lease") and method == "DELETE":
            return FakeResponse({"ok": True, "released": True})
        if path.endswith("/commit") and method == "POST":
            if self.fail_next_commit:
                self.fail_next_commit = False
                raise OSError("response lost")
            commit_id = json_body["commit_id"]
            if commit_id not in self.commit_results:
                self.commit_results[commit_id] = {
                    "ok": True, "commit_id": commit_id, "revision": "r:5", "replayed": False,
                }
            else:
                return FakeResponse({**self.commit_results[commit_id], "replayed": True})
            return FakeResponse(self.commit_results[commit_id])
        raise AssertionError((method, path, json_body))

    async def exec_tool(self, name, arguments, *, workspace=""):
        self.exec_calls.append((name, copy.deepcopy(arguments), workspace))
        return {"host": self.host_id, "ok": True}


def _resolver(*sessions):
    by_id = {session.host_id: session for session in sessions}
    return lambda host_id: by_id.get(host_id)


@pytest.mark.asyncio
async def test_begin_and_commit_are_bound_to_requested_owner_and_idempotent():
    host_a = FakeSession("host-a")
    host_b = FakeSession("host-b")
    resolve = _resolver(host_a, host_b)

    turn = await begin_remote_turn(
        "host-a", "731", workspace="remote:host-b:C:/shared",
        session_resolver=resolve,
    )
    assert turn.identity.owner_id == "host-a"
    assert turn.identity.conversation_id == "731"
    assert turn.identity.workspace_owner_id == "host-b"
    assert [call[:2] for call in host_a.calls] == [
        ("GET", "/api/remote/conversations/731/snapshot"),
        ("POST", "/api/remote/conversations/731/lease"),
    ]
    assert host_b.calls == []

    turn.append_message({"role": "assistant", "content": "draft"})
    turn.update_message("12", content="edited in memory")
    assert host_a.calls[-1][1].endswith("/lease")  # transcript isn't persisted mid-turn

    host_a.fail_next_commit = True
    with pytest.raises(RemoteTurnUnavailable):
        await turn.commit()
    first_attempt = host_a.calls[-1][2]
    assert first_attempt["messages"] == [
        {"id": 12, "role": "user", "content": "edited in memory"},
        {"role": "assistant", "content": "draft"},
    ]

    # Retry uses the same commit ID and byte-for-byte payload after an uncertain outcome.
    result = await turn.commit()
    retry = host_a.calls[-1][2]
    assert retry == first_attempt
    assert result["revision"] == "r:5"
    assert retry["commit_id"] == first_attempt["commit_id"]
    await turn.release()
    assert host_a.calls[-1][0:2] == (
        "DELETE", "/api/remote/conversations/731/lease",
    )


@pytest.mark.asyncio
async def test_workspace_tools_route_only_to_workspace_owner_and_local_calls_stay_local():
    conversation_owner = FakeSession("conversation-host")
    workspace_owner = FakeSession("workspace-host")
    turn = await begin_remote_turn(
        "conversation-host", "731", workspace="remote:workspace-host:C:/repo",
        session_resolver=_resolver(conversation_owner, workspace_owner),
    )
    local_calls = []

    async def local_dispatch(name, arguments, workspace):
        local_calls.append((name, arguments, workspace))
        return {"local": True}

    result = await turn.dispatch_tool("read_file", {"path": "a.py"}, local_dispatch)
    assert result == {"host": "workspace-host", "ok": True}
    assert workspace_owner.exec_calls == [
        ("read_file", {"path": "a.py"}, "remote:workspace-host:C:/repo"),
    ]
    assert conversation_owner.exec_calls == []

    turn.identity  # identity is immutable and continues to represent this turn
    local_result = await turn.dispatch_tool("get_help", {"topic": "x"}, local_dispatch)
    assert local_result == {"local": True}
    assert local_calls == [("get_help", {"topic": "x"}, "remote:workspace-host:C:/repo")]
    await turn.release()


@pytest.mark.asyncio
async def test_unknown_owner_offline_and_lease_or_revision_conflicts_fail_closed():
    with pytest.raises(RemoteTurnUnavailable):
        await begin_remote_turn("missing", "731", session_resolver=lambda _: None)

    host = FakeSession("host-a")
    host.response_overrides[("POST", "/api/remote/conversations/731/lease")] = FakeResponse(
        {"detail": {"code": "stale_revision"}}, status_code=409,
    )
    with pytest.raises(RemoteTurnConflict):
        await begin_remote_turn("host-a", "731", session_resolver=_resolver(host))
    assert not any(path.endswith("/commit") for _, path, _ in host.calls)

    offline = FakeSession("host-a")
    async def disconnected(*args, **kwargs):
        raise OSError("offline")
    offline.proxy = disconnected
    with pytest.raises(RemoteTurnUnavailable):
        await begin_remote_turn("host-a", "731", session_resolver=_resolver(offline))


@pytest.mark.asyncio
async def test_workspace_owner_mismatch_is_rejected_before_any_tool_or_commit():
    owner = FakeSession("host-a")
    with pytest.raises(ValueError, match="workspace owner"):
        await begin_remote_turn(
            "host-a", "731", workspace="remote:host-b:C:/repo",
            workspace_owner_id="host-a", session_resolver=_resolver(owner),
        )
    assert owner.calls == []


@pytest.mark.asyncio
async def test_lease_can_be_renewed_and_context_manager_releases_lease():
    owner = FakeSession("host-a")
    async with await begin_remote_turn("host-a", "731", session_resolver=_resolver(owner)) as turn:
        renewed = await turn.renew_lease()
        assert renewed["lease_token"] == "lease-host-a"
        assert owner.calls[-1][0:2] == (
            "POST", "/api/remote/conversations/731/lease",
        )
    assert owner.calls[-1][0:2] == (
        "DELETE", "/api/remote/conversations/731/lease",
    )
