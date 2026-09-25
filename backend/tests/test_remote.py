"""Remote hosting: auth gate, host exec endpoint, client dispatch split,
handshake protocol check, discovery beacon properties."""

import pytest
from httpx import ASGITransport, AsyncClient

from backend.agent import remote as remote_mod
from backend.agent.config import CONFIG_PATH, load_config, save_config
from backend.main import app


@pytest.fixture(autouse=True)
def _no_remote_session():
    remote_mod.clear_remote()
    saved_devices = load_config().get("remote_devices", [])
    save_config({"remote_devices": []})
    yield
    remote_mod.clear_remote()
    save_config({"remote_devices": saved_devices})


def _set_host(passphrase: str):
    """Point the redirected test config's remote block at a host state."""
    save_config(
        {"remote": {**load_config().get("remote", {}), "passphrase": passphrase}}
    )


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_remote_device_conversation_cache_refreshes_and_serves_offline(monkeypatch):
    import httpx
    from backend.db.database import get_remote_messages

    host_id = "chat-host"
    session = remote_mod.RemoteSession("http://host", "secret", {
        "host_id": host_id, "hostname": "Host", "protocol": remote_mod.PROTOCOL_VERSION,
    })
    remote_mod.register_remote(session)
    save_config({"remote_devices": [{"host_id": host_id, "url": "http://host", "cached_workspaces": []}]})

    async def proxy(method, path, **kwargs):
        if path == "/api/remote/conversations":
            return httpx.Response(200, json=[{"id": 3, "title": "Remote chat", "updated_at": "2025-01-01"}])
        if path.endswith("/messages"):
            return httpx.Response(200, json=[{"id": 9, "role": "user", "content": "cached"}])
        raise AssertionError(path)

    monkeypatch.setattr(session, "proxy", proxy)
    async with await _client() as c:
        r = await c.get(f"/api/remote/devices/{host_id}/conversations")
        assert r.status_code == 200
        assert r.json()["conversations"][0]["conversation_id"] == "3"
        assert (await c.get(f"/api/remote/devices/{host_id}/conversations/3/messages")).json()[0]["content"] == "cached"
        monkeypatch.setattr(session, "proxy", lambda *a, **k: None)
        async def offline(*a, **k):
            raise httpx.ConnectError("offline")
        monkeypatch.setattr(session, "proxy", offline)
        r = await c.get(f"/api/remote/devices/{host_id}/conversations/3/messages")
        assert r.status_code == 200
        assert r.json()[0]["content"] == "cached"
        assert await get_remote_messages(host_id, "3") is not None


@pytest.mark.asyncio
async def test_remote_image_proxy_uses_only_saved_owner_session(monkeypatch):
    import httpx

    host_a = remote_mod.RemoteSession("http://host-a", "secret-a", {"host_id": "host-a"})
    host_b = remote_mod.RemoteSession("http://host-b", "secret-b", {"host_id": "host-b"})
    remote_mod.register_remote(host_a)
    remote_mod.register_remote(host_b)
    save_config({"remote_devices": [
        {"host_id": "host-a", "url": "http://host-a"},
        {"host_id": "host-b", "url": "http://host-b"},
    ]})
    calls = []

    async def proxy_a(method, path, **kwargs):
        calls.append(("a", method, path, kwargs))
        return httpx.Response(200, content=b"image from a", headers={"content-type": "image/png"})

    async def proxy_b(method, path, **kwargs):
        calls.append(("b", method, path, kwargs))
        return httpx.Response(200, content=b"image from b", headers={"content-type": "image/png"})

    monkeypatch.setattr(host_a, "proxy", proxy_a)
    monkeypatch.setattr(host_b, "proxy", proxy_b)

    async with await _client() as c:
        response_a = await c.get("/api/remote/devices/host-a/images/3/capture.png")
        response_b = await c.get("/api/remote/devices/host-b/images/3/capture.png")

    assert response_a.status_code == response_b.status_code == 200
    assert response_a.content == b"image from a"
    assert response_b.content == b"image from b"
    assert response_a.headers["content-type"] == "image/png"
    assert response_a.headers["x-content-type-options"] == "nosniff"
    assert b"secret-a" not in response_a.content
    assert "secret-a" not in str(response_a.headers)
    assert [(owner, method, path) for owner, method, path, _ in calls] == [
        ("a", "GET", "/api/images/3/capture.png"),
        ("b", "GET", "/api/images/3/capture.png"),
    ]


@pytest.mark.asyncio
async def test_remote_image_proxy_rejects_path_traversal_before_proxy(monkeypatch):
    host = remote_mod.RemoteSession("http://host", "secret", {"host_id": "host-a"})
    remote_mod.register_remote(host)
    save_config({"remote_devices": [{"host_id": "host-a", "url": "http://host"}]})

    async def should_not_proxy(*args, **kwargs):
        raise AssertionError("invalid path must not be proxied")

    monkeypatch.setattr(host, "proxy", should_not_proxy)
    async with await _client() as c:
        response = await c.get("/api/remote/devices/host-a/images/%2e%2e%2fsecret.png")

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_remote_image_proxy_rejects_unsaved_and_foreign_owner_ids(monkeypatch):
    host = remote_mod.RemoteSession("http://host", "secret", {"host_id": "host-a"})
    remote_mod.register_remote(host)
    save_config({"remote_devices": [{"host_id": "host-a", "url": "http://host"}]})
    calls = []

    async def proxy(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(host, "proxy", proxy)
    async with await _client() as c:
        unknown = await c.get("/api/remote/devices/host-b/images/3/image.png")
        assert unknown.status_code == 404

    assert calls == []


# ---------------------------------------------------------------- auth gate


@pytest.mark.asyncio
async def test_local_requests_need_no_auth():
    async with await _client() as c:
        assert (await c.get("/api/health")).status_code == 200


@pytest.mark.asyncio
async def test_remote_marker_without_passphrase_is_refused():
    _set_host("hunter2")
    async with await _client() as c:
        res = await c.post(
            "/api/remote/exec",
            json={"name": "bash", "args": {"command": "echo hi"}},
            headers={"X-Yaah-Remote": "1"},
        )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_remote_wrong_passphrase_is_refused():
    _set_host("hunter2")
    async with await _client() as c:
        res = await c.post(
            "/api/remote/exec",
            json={"name": "bash", "args": {"command": "echo hi"}},
            headers={"X-Yaah-Remote": "1", "X-Yaah-Passphrase": "wrong"},
        )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_host_without_passphrase_refuses_all_remote_access():
    _set_host("")
    async with await _client() as c:
        res = await c.post(
            "/api/remote/exec",
            json={"name": "bash", "args": {"command": "echo hi"}},
            headers={"X-Yaah-Remote": "1", "X-Yaah-Passphrase": ""},
        )
    assert res.status_code == 401
    assert "no passphrase set" in res.json()["detail"]


@pytest.mark.asyncio
async def test_env_passphrase_overrides_config(monkeypatch):
    """The Linux daemon's /etc/yaah/yaah.conf arrives as YAAH_PASSPHRASE via
    the unit's EnvironmentFile and must win over ~/.yaah/config.json."""
    _set_host("from-config")
    monkeypatch.setenv("YAAH_PASSPHRASE", "from-env")
    async with await _client() as c:
        ok = await c.post(
            "/api/remote/exec",
            json={"name": "bash", "args": {"command": "echo hi"}},
            headers={"X-Yaah-Remote": "1", "X-Yaah-Passphrase": "from-env"},
        )
        stale = await c.post(
            "/api/remote/exec",
            json={"name": "bash", "args": {"command": "echo hi"}},
            headers={"X-Yaah-Remote": "1", "X-Yaah-Passphrase": "from-config"},
        )
    assert ok.status_code != 401
    assert stale.status_code == 401


# ---------------------------------------------------------------- exec endpoint


@pytest.mark.asyncio
async def test_exec_runs_workspace_tool_in_host_default_workspace(monkeypatch):
    _set_host("hunter2")
    seen = {}

    async def fake_bash(workspace, command, timeout_seconds=60):
        seen["call"] = ("bash", {"command": command}, workspace)
        return {"exit_code": 0, "output": "hi"}

    from backend.agent import tools as tools_mod

    monkeypatch.setitem(tools_mod.EXECUTORS, "bash", fake_bash)
    async with await _client() as c:
        res = await c.post(
            "/api/remote/exec",
            json={"name": "bash", "args": {"command": "echo hi"}},
            headers={"X-Yaah-Remote": "1", "X-Yaah-Passphrase": "hunter2"},
        )
    assert res.status_code == 200
    assert res.json() == {"exit_code": 0, "output": "hi"}
    # The host resolves the workspace itself ("" = its default/home); a
    # client path must never be trusted here.
    assert seen["call"] == ("bash", {"command": "echo hi"}, "")


@pytest.mark.asyncio
async def test_exec_rejects_non_workspace_tools():
    _set_host("hunter2")
    async with await _client() as c:
        res = await c.post(
            "/api/remote/exec",
            json={"name": "web_search", "args": {"query": "x"}},
            headers={"X-Yaah-Remote": "1", "X-Yaah-Passphrase": "hunter2"},
        )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_remote_info_is_open_and_names_the_protocol():
    async with await _client() as c:
        res = await c.get("/api/remote/info")
    body = res.json()
    assert body["protocol"] == remote_mod.PROTOCOL_VERSION
    assert body["hostname"]
    assert isinstance(body["windows"], bool)


# ------------------------------------------------------- remote edit protocol


@pytest.fixture
async def remote_conversation():
    from backend.db.database import add_message, create_conversation

    conversation_id = await create_conversation("Before edit")
    await add_message(conversation_id, "user", "original")
    return conversation_id


_REMOTE_HEADERS = {"X-Yaah-Remote": "1", "X-Yaah-Passphrase": "hunter2"}


@pytest.mark.asyncio
async def test_remote_snapshot_lease_commit_is_idempotent_and_preserves_ids(remote_conversation):
    from backend.db.database import get_messages

    _set_host("hunter2")
    cid = remote_conversation
    async with await _client() as c:
        snapshot_response = await c.get(
            f"/api/remote/conversations/{cid}/snapshot", headers=_REMOTE_HEADERS
        )
        assert snapshot_response.status_code == 200
        snapshot = snapshot_response.json()
        assert snapshot["revision"].startswith("r:")
        assert snapshot["conversation"]["revision"] == snapshot["revision"]
        message_id = snapshot["messages"][0]["id"]
        lease_response = await c.post(
            f"/api/remote/conversations/{cid}/lease",
            headers=_REMOTE_HEADERS,
            json={"revision": snapshot["revision"], "holder_id": "client-a"},
        )
        assert lease_response.status_code == 200
        lease = lease_response.json()
        assert lease["lease_seconds"] == 120
        assert lease["expires_at"] > 0
        body = {
            "lease_token": lease["lease_token"],
            "revision": snapshot["revision"],
            "commit_id": "commit-once",
            "conversation": {"title": "After edit", "workspace": "C:/repo"},
            "messages": [
                {**snapshot["messages"][0], "content": "edited original"},
                {"id": 99, "role": "assistant", "content": "reply"},
            ],
        }
        first = await c.post(
            f"/api/remote/conversations/{cid}/commit", headers=_REMOTE_HEADERS, json=body
        )
        replay = await c.post(
            f"/api/remote/conversations/{cid}/commit", headers=_REMOTE_HEADERS, json=body
        )
        assert first.status_code == 200
        assert replay.status_code == 200
        assert first.json()["replayed"] is False
        assert replay.json()["replayed"] is True
        assert first.json()["revision"] == replay.json()["revision"]
        assert (await get_messages(cid))[0]["id"] == message_id
        assert [m["content"] for m in await get_messages(cid)] == ["edited original", "reply"]
        refreshed = await c.get(
            f"/api/remote/conversations/{cid}/snapshot", headers=_REMOTE_HEADERS
        )
        assert refreshed.json()["revision"] == first.json()["revision"]


@pytest.mark.asyncio
async def test_remote_edit_lease_conflict_stale_revision_and_independent_conversations(remote_conversation):
    from backend.db.database import create_conversation

    _set_host("hunter2")
    cid = remote_conversation
    other_id = await create_conversation("Independent")
    async with await _client() as c:
        snapshot = (await c.get(
            f"/api/remote/conversations/{cid}/snapshot", headers=_REMOTE_HEADERS
        )).json()
        stale = await c.post(
            f"/api/remote/conversations/{cid}/lease", headers=_REMOTE_HEADERS,
            json={"revision": "r:999"},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "stale_revision"
        acquired = await c.post(
            f"/api/remote/conversations/{cid}/lease", headers=_REMOTE_HEADERS,
            json={"revision": snapshot["revision"], "holder_id": "first"},
        )
        assert acquired.status_code == 200
        held = await c.post(
            f"/api/remote/conversations/{cid}/lease", headers=_REMOTE_HEADERS,
            json={"revision": snapshot["revision"], "holder_id": "second"},
        )
        assert held.status_code == 423
        assert held.json()["detail"]["code"] == "lease_held"
        bypass = await c.post(
            f"/api/conversations/{cid}/messages", headers=_REMOTE_HEADERS,
            json={"role": "user", "content": "must not bypass lease"},
        )
        assert bypass.status_code == 409
        assert bypass.json()["detail"]["code"] == "remote_write_requires_lease"
        other_snapshot = (await c.get(
            f"/api/remote/conversations/{other_id}/snapshot", headers=_REMOTE_HEADERS
        )).json()
        independent = await c.post(
            f"/api/remote/conversations/{other_id}/lease", headers=_REMOTE_HEADERS,
            json={"revision": other_snapshot["revision"]},
        )
        assert independent.status_code == 200

        # An ordinary host-side edit bumps the revision, invalidating the old
        # token without allowing the stale snapshot to overwrite it.
        from backend.db.database import update_conversation
        await update_conversation(cid, title="Host changed")
        commit = await c.post(
            f"/api/remote/conversations/{cid}/commit", headers=_REMOTE_HEADERS,
            json={"lease_token": acquired.json()["lease_token"],
                  "revision": snapshot["revision"], "commit_id": "stale-commit",
                  "conversation": {"title": "Lost host edit"}, "messages": snapshot["messages"]},
        )
        assert commit.status_code == 409
        assert commit.json()["detail"]["code"] == "stale_revision"


@pytest.mark.asyncio
async def test_remote_lease_renew_release_expiry_and_auth(remote_conversation):
    import time
    from backend.db.database import get_db

    _set_host("hunter2")
    cid = remote_conversation
    async with await _client() as c:
        denied = await c.get(
            f"/api/remote/conversations/{cid}/snapshot",
            headers={"X-Yaah-Remote": "1"},
        )
        assert denied.status_code == 401
        snapshot = (await c.get(
            f"/api/remote/conversations/{cid}/snapshot", headers=_REMOTE_HEADERS
        )).json()
        acquired = await c.post(
            f"/api/remote/conversations/{cid}/lease", headers=_REMOTE_HEADERS,
            json={"revision": snapshot["revision"]},
        )
        token = acquired.json()["lease_token"]
        renewed = await c.post(
            f"/api/remote/conversations/{cid}/lease", headers=_REMOTE_HEADERS,
            json={"lease_token": token},
        )
        assert renewed.status_code == 200
        assert renewed.json()["expires_at"] >= acquired.json()["expires_at"]
        released = await c.request(
            "DELETE", f"/api/remote/conversations/{cid}/lease",
            headers=_REMOTE_HEADERS, json={"lease_token": token},
        )
        assert released.status_code == 200
        assert released.json() == {"ok": True, "released": True}
        expired_lease = await c.post(
            f"/api/remote/conversations/{cid}/lease", headers=_REMOTE_HEADERS,
            json={"revision": snapshot["revision"]},
        )
        db = await get_db()
        try:
            await db.execute(
                "UPDATE remote_edit_leases SET expires_at = ? WHERE conversation_id = ?",
                (int(time.time()) - 1, cid),
            )
            await db.commit()
        finally:
            await db.close()
        next_lease = await c.post(
            f"/api/remote/conversations/{cid}/lease", headers=_REMOTE_HEADERS,
            json={"revision": snapshot["revision"]},
        )
        assert next_lease.status_code == 200
        # Expiry is enforced during release as well as on renew/commit.
        expired_release = await c.request(
            "DELETE", f"/api/remote/conversations/{cid}/lease",
            headers=_REMOTE_HEADERS,
            json={"lease_token": expired_lease.json()["lease_token"]},
        )
        assert expired_release.status_code == 409
        rejected = await c.post(
            f"/api/remote/conversations/{cid}/commit", headers=_REMOTE_HEADERS,
            json={"lease_token": expired_lease.json()["lease_token"],
                  "revision": snapshot["revision"], "commit_id": "expired",
                  "conversation": {}, "messages": snapshot["messages"]},
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "lease_invalid_or_expired"


# ---------------------------------------------------------------- client dispatch


class _StubSession(remote_mod.RemoteSession):
    """Minimal RemoteSession stand-in: real proxy()/headers, stubbed exec."""

    def __init__(self, windows=False, result=None, host_id=None):
        super().__init__(
            "http://host:8765",
            "p",
            {
                "os": "Linux",
                "os_version": "6",
                "machine": "x86_64",
                "windows": windows,
                "workspace_root": "/home/host",
                "hostname": "stub",
                "host_id": host_id,
            },
        )
        self.result = result if result is not None else {"ok": True}
        self.calls = []

    async def exec_tool(self, name, args, workspace=""):
        self.calls.append((name, args, workspace))
        return self.result


@pytest.mark.asyncio
async def test_workspace_tools_route_to_remote(monkeypatch):
    stub = _StubSession(result={"exit_code": 0, "output": "remote!"})
    remote_mod.set_remote(stub)
    from backend.agent.tools import execute_tool

    result = await execute_tool("bash", {"command": "echo hi"}, "C:/local/ws")
    assert result == {"exit_code": 0, "output": "remote!"}
    assert stub.calls == [("bash", {"command": "echo hi"}, "C:/local/ws")]


@pytest.mark.asyncio
async def test_remote_workspace_tools_route_to_each_registered_host():
    host_a = _StubSession(result={"host": "a"}, host_id="host-a")
    host_b = _StubSession(result={"host": "b"}, host_id="host-b")
    remote_mod.register_remote(host_a)
    remote_mod.register_remote(host_b)
    from backend.agent.tools import execute_tool

    a = await execute_tool("bash", {"command": "pwd"}, "remote:host-a:/srv/a")
    b = await execute_tool("bash", {"command": "pwd"}, "remote:host-b:/srv/b")

    assert a == {"host": "a"}
    assert b == {"host": "b"}
    assert host_a.calls == [("bash", {"command": "pwd"}, "remote:host-a:/srv/a")]
    assert host_b.calls == [("bash", {"command": "pwd"}, "remote:host-b:/srv/b")]


@pytest.mark.asyncio
async def test_unregistered_remote_workspace_fails_closed():
    from backend.agent.tools import execute_tool

    result = await execute_tool("bash", {"command": "pwd"}, "remote:missing:/srv/app")

    assert "no connected remote device" in result["error"]


@pytest.mark.asyncio
async def test_registered_hosts_do_not_redirect_local_workspace_tools(monkeypatch):
    host = _StubSession(result={"host": "remote"}, host_id="host-a")
    remote_mod.register_remote(host)
    seen = {}

    async def local_bash(workspace, command, timeout_seconds=60):
        seen["call"] = (workspace, command)
        return {"output": "local"}

    from backend.agent import tools as tools_mod

    monkeypatch.setitem(tools_mod.EXECUTORS, "bash", local_bash)
    from backend.agent.tools import execute_tool

    result = await execute_tool("bash", {"command": "pwd"}, "C:/local/project")

    assert result == {"output": "local"}
    assert seen["call"] == ("C:/local/project", "pwd")
    assert host.calls == []


@pytest.mark.asyncio
async def test_exec_forwards_selected_workspace(monkeypatch):
    """/api/remote/exec runs the tool in the workspace the client selected
    (v2 protocol) — never silently in the host's home."""
    _set_host("hunter2")
    seen = {}

    async def fake_bash(workspace, command, timeout_seconds=60):
        seen["ws"] = workspace
        return {"exit_code": 0, "output": command}

    from backend.agent import tools as tools_mod

    monkeypatch.setitem(tools_mod.EXECUTORS, "bash", fake_bash)
    async with await _client() as c:
        res = await c.post(
            "/api/remote/exec",
            json={
                "name": "bash",
                "args": {"command": "pwd"},
                "workspace": "/srv/proj",
            },
            headers={"X-Yaah-Remote": "1", "X-Yaah-Passphrase": "hunter2"},
        )
    assert res.status_code == 200
    assert seen["ws"] == "/srv/proj"


@pytest.mark.asyncio
async def test_add_workspace_proxies_host_path_raw(monkeypatch):
    """Host-bound paths must NOT be normalized with the client's OS rules:
    a Linux path typed on a Windows client (/proj or proj) would otherwise
    be rewritten to C:\\Users\\... before it ever reaches the host."""
    from backend.agent import remote as rm

    host = _StubSession()
    host.info["host_id"] = host.host_id = "abc123"
    _FakeProxyClient.response_body = {
        "id": 9,
        "path": "/home/derp/proj",
        "label": "proj",
        "exists": True,
        "last_opened_at": None,
        "conversation_count": 0,
    }
    monkeypatch.setattr(rm.httpx, "AsyncClient", _FakeProxyClient)
    remote_mod.register_remote(host)
    async with await _client() as c:
        row = (
            await c.post(
                "/api/workspaces",
                json={"path": "remote:abc123:proj", "owner_id": "abc123"},
            )
        ).json()
    sent = _FakeProxyClient.last_request
    assert sent["json"]["path"] == "proj"  # raw, untouched by client rules
    assert row["path"] == "remote:abc123:/home/derp/proj"  # namespaced reply


@pytest.mark.asyncio
async def test_exec_tool_strips_own_namespace_and_refuses_foreign():
    # A REAL RemoteSession (not _StubSession, whose exec_tool is stubbed out)
    host = remote_mod.RemoteSession(
        "http://host:8765",
        "p",
        {
            "os": "Linux",
            "os_version": "6",
            "machine": "x86_64",
            "windows": False,
            "workspace_root": "/home/host",
            "hostname": "stub",
            "host_id": "abc123",
        },
    )
    host.host_id = "abc123"

    captured = {}

    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["json"] = json
            return _FakeResponse({"ok": True})

    from backend.agent import remote as rm

    orig = rm.httpx.AsyncClient
    rm.httpx.AsyncClient = _C
    try:
        ok = await host.exec_tool(
            "bash", {"command": "pwd"}, workspace="remote:abc123:/srv/x"
        )
        assert ok == {"ok": True}
        assert captured["json"]["workspace"] == "/srv/x"  # namespace stripped

        bad = await host.exec_tool(
            "bash", {"command": "pwd"}, workspace="remote:other:/x"
        )
        assert "different remote host" in bad["error"]
    finally:
        rm.httpx.AsyncClient = orig


def test_env_line_names_selected_workspace():
    host = _StubSession()
    host.info["host_id"] = host.host_id = "abc123"
    remote_mod.set_remote(host)

    line = host.env_line("remote:abc123:/srv/proj")
    assert "/srv/proj" in line
    assert "default workspace" not in line

    # Host Default (empty path) or a foreign namespace falls back to home
    assert host.env_line("remote:abc123:") == host.env_line("")
    assert "/home/host" in host.env_line("remote:other:/x")

    remote_mod.clear_remote()


@pytest.mark.asyncio
async def test_local_only_tools_stay_local(monkeypatch):
    stub = _StubSession()
    remote_mod.set_remote(stub)
    from backend.agent import tools as tools_mod
    from backend.agent.webtools import web_search

    called = {"n": 0}

    async def fake_web_search(workspace, **kwargs):
        called["n"] += 1
        return {"results": []}

    monkeypatch.setitem(tools_mod.EXECUTORS, "web_search", fake_web_search)
    monkeypatch.setattr("backend.main.tools_mod.web_search", web_search)
    from backend.agent.tools import execute_tool

    await execute_tool("web_search", {"query": "x"}, "ws")
    assert stub.calls == []  # never forwarded
    assert called["n"] == 1


def test_schemas_follow_host_platform():
    from backend.agent.tools import get_schemas

    names_local = {s["function"]["name"] for s in get_schemas()}

    remote_mod.set_remote(_StubSession(windows=False))
    linux_names = {s["function"]["name"] for s in get_schemas()}

    remote_mod.set_remote(_StubSession(windows=True))
    windows_names = {s["function"]["name"] for s in get_schemas()}

    remote_mod.clear_remote()
    # Even on a Windows dev machine (this repo's CI is Windows), a Linux
    # host must hide powershell and a Windows host must show it.
    if names_local != linux_names or names_local == windows_names:
        assert "powershell" not in linux_names
        assert "powershell" in windows_names


# ---------------------------------------------------------------- connect handshake


class _FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._body


class _FakeAsyncClient:
    body: dict = {}
    status = 200

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        if url.endswith("/api/remote/verify"):
            return _FakeResponse({"ok": True}, getattr(self, "verify_status", 200))
        return _FakeResponse(self.body, self.status)


@pytest.mark.asyncio
async def test_connect_accepts_matching_protocol(monkeypatch):
    import backend.main as main_mod

    body = remote_mod.host_info()
    body["protocol"] = remote_mod.PROTOCOL_VERSION
    body["instance_id"] = "some-other-instance"
    monkeypatch.setattr(main_mod.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.body = body
    async with await _client() as c:
        res = await c.post(
            "/api/remote/connect",
            json={"url": "http://10.0.0.5:8765", "passphrase": "p"},
        )
    assert res.status_code == 200
    assert res.json()["connected"] is True
    assert res.json()["name"] == body["hostname"]


@pytest.mark.asyncio
async def test_connect_refuses_protocol_mismatch(monkeypatch):
    import backend.main as main_mod

    body = remote_mod.host_info()
    body["protocol"] = remote_mod.PROTOCOL_VERSION + 1
    monkeypatch.setattr(main_mod.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.body = body
    async with await _client() as c:
        res = await c.post(
            "/api/remote/connect",
            json={"url": "http://10.0.0.5:8765", "passphrase": "p"},
        )
    assert res.status_code == 409
    assert "Incompatible" in res.json()["detail"]
    assert remote_mod.get_remote() is None


@pytest.mark.asyncio
async def test_connect_unreachable_host(monkeypatch):
    import backend.main as main_mod

    class _Down(_FakeAsyncClient):
        async def get(self, url):
            raise main_mod.httpx.ConnectError("refused")

    monkeypatch.setattr(main_mod.httpx, "AsyncClient", _Down)
    async with await _client() as c:
        res = await c.post(
            "/api/remote/connect",
            json={"url": "http://10.0.0.5:8765", "passphrase": "p"},
        )
    assert res.status_code == 502


# ---------------------------------------------------------------- discovery beacon


def test_beacon_props_reflect_config():
    from backend.agent import discovery

    _set_host("hunter2")
    props = discovery.beacon_props()
    assert props["proto"] == str(remote_mod.PROTOCOL_VERSION)
    assert props["auth"] == "1"
    _set_host("")
    assert discovery.beacon_props()["auth"] == "0"


# ---------------------------------------------------------------- files proxy


class _FakeProxyClient:
    """Stands in for the httpx client inside RemoteSession.proxy."""

    last_request: dict = {}
    response_status = 200
    response_body: dict = {}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, params=None, json=None, headers=None):
        _FakeProxyClient.last_request = {
            "method": method,
            "url": url,
            "params": params,
            "json": json,
            "headers": headers,
        }
        return _FakeResponse(
            _FakeProxyClient.response_body, _FakeProxyClient.response_status
        )


@pytest.mark.asyncio
async def test_files_tree_proxies_to_host_when_connected(monkeypatch):
    from backend.agent import remote as rm

    _FakeProxyClient.response_body = {"root": "/home/host", "tree": []}
    monkeypatch.setattr(rm.httpx, "AsyncClient", _FakeProxyClient)
    host = _StubSession(host_id="legacy-host")
    remote_mod.set_remote(host)
    async with await _client() as c:
        res = await c.get(
            "/api/files", params={"workspace": "remote:legacy-host:/repo"}
        )
    assert res.status_code == 200
    assert res.json() == {"root": "/home/host", "tree": []}
    sent = _FakeProxyClient.last_request
    assert sent["method"] == "GET"
    assert sent["url"].endswith("/api/files")
    # The remote-marker headers ride along, so the host's auth gate applies.
    assert sent["headers"]["X-Yaah-Remote"] == "1"
    assert sent["headers"]["X-Yaah-Passphrase"] == "p"


@pytest.mark.asyncio
async def test_attachments_proxied_to_host(monkeypatch):
    from backend.agent import remote as rm

    _FakeProxyClient.response_body = {"path": ".yaah-attachments/a.txt"}
    monkeypatch.setattr(rm.httpx, "AsyncClient", _FakeProxyClient)
    host = _StubSession(host_id="attachment-host")
    remote_mod.register_remote(host)
    async with await _client() as c:
        res = await c.post(
            "/api/attachments",
            json={
                "workspace": "remote:attachment-host:/repo",
                "name": "a.txt",
                "content": "hi",
            },
        )
    assert res.json() == {"path": ".yaah-attachments/a.txt"}
    assert _FakeProxyClient.last_request["json"]["content"] == "hi"
    assert _FakeProxyClient.last_request["json"]["workspace"] == "/repo"


@pytest.mark.asyncio
async def test_connect_refuses_this_same_instance(monkeypatch):
    import backend.main as main_mod

    body = remote_mod.host_info()
    body["protocol"] = remote_mod.PROTOCOL_VERSION
    monkeypatch.setattr(main_mod.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.body = body
    async with await _client() as c:
        res = await c.post(
            "/api/remote/connect",
            json={"url": "http://127.0.0.1:8765", "passphrase": "p"},
        )
    assert res.status_code == 400
    assert "same instance" in res.json()["detail"]
    assert remote_mod.get_remote() is None


# ---------------------------------------------------------------- loopback gate


def test_loopback_classification():
    from backend.main import _is_loopback_client

    assert _is_loopback_client("127.0.0.1")
    assert _is_loopback_client("::1")
    assert _is_loopback_client("")
    assert _is_loopback_client("testclient")  # ASGI test transport
    assert not _is_loopback_client("192.168.1.38")
    assert not _is_loopback_client("10.0.0.5")


# ---------------------------------------------------------------- host id + namespacing


def test_host_id_is_stable_and_namespacing_roundtrips():
    from backend.agent import remote as rm

    hid = rm.ensure_host_id()
    assert hid and rm.ensure_host_id() == hid
    assert rm.host_info()["host_id"] == hid

    ns = rm.ns_path(hid, "C:/repo")
    assert ns == f"remote:{hid}:C:/repo"
    assert rm.parse_ns(ns) == (hid, "C:/repo")
    # Host Default (empty path) and local strings
    assert rm.parse_ns(rm.ns_path(hid, "")) == (hid, "")
    assert rm.parse_ns("C:/repo") is None
    assert rm.parse_ns(None) is None
    assert (
        "hid"
        in __import__(
            "backend.agent.discovery", fromlist=["beacon_props"]
        ).beacon_props()
    )


@pytest.mark.asyncio
async def test_remote_url_rejects_embedded_credentials():
    async with await _client() as c:
        res = await c.post(
            "/api/remote/devices",
            json={"url": "http://user:secret@10.0.0.5:8765", "passphrase": "p"},
        )
    assert res.status_code == 400
    assert "embedded credentials" in res.json()["detail"]


@pytest.mark.asyncio
async def test_device_connect_saves_profile_without_changing_active_scope(monkeypatch):

    import json
    import backend.main as main_mod

    body = {
        **remote_mod.host_info(),
        "host_id": "saved-host",
        "protocol": remote_mod.PROTOCOL_VERSION,
        "instance_id": "remote-one",
        "app_version": "1",
    }
    _FakeAsyncClient.body = body
    _FakeAsyncClient.verify_status = 200
    monkeypatch.setattr(main_mod.httpx, "AsyncClient", _FakeAsyncClient)
    active = _StubSession(host_id="legacy-active")
    remote_mod.register_remote(active, make_active=True)
    async with await _client() as c:
        res = await c.post(
            "/api/remote/devices",
            json={"url": "http://10.0.0.5:8765", "passphrase": "secret"},
        )
        legacy_status = await c.get("/api/remote/status")
    assert legacy_status.json()["host_id"] == "legacy-active"
    assert res.status_code == 200
    assert res.json()["status"] == "online"
    assert remote_mod.get_remote() is active
    assert remote_mod.get_remote("saved-host").passphrase == "secret"
    async with await _client() as c:
        await c.post("/api/remote/devices/saved-host/disconnect")
        after_disconnect = await c.get("/api/remote/status")
    assert after_disconnect.json()["host_id"] == "legacy-active"
    assert remote_mod.get_remote("saved-host") is None
    assert remote_mod.get_remote() is active
    profiles = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["remote_devices"]
    assert profiles[0]["host_id"] == "saved-host"
    assert "passphrase" not in profiles[0]


@pytest.mark.asyncio
async def test_workspaces_mirror_host_registry_namespaced(monkeypatch):
    from backend.agent import remote as rm

    host = _StubSession()
    host.info["host_id"] = host.host_id = "abc123"
    _FakeProxyClient.response_body = [
        {
            "id": 1,
            "path": None,
            "label": "Default (Home)",
            "exists": True,
            "last_opened_at": None,
            "conversation_count": 0,
        },
        {
            "id": 2,
            "path": "/home/host/proj",
            "label": "proj",
            "exists": True,
            "last_opened_at": None,
            "conversation_count": 0,
        },
    ]
    monkeypatch.setattr(rm.httpx, "AsyncClient", _FakeProxyClient)
    remote_mod.set_remote(host)
    async with await _client() as c:
        rows = (await c.get("/api/workspaces")).json()
    remote_rows = [row for row in rows if row.get("owner_id") == "abc123"]
    assert remote_rows[0]["path"] == "remote:abc123:"
    assert remote_rows[1]["path"] == "remote:abc123:/home/host/proj"
    # Raw host paths must never leak to the client UI as-is. Local Default
    # remains an ordinary null path in the aggregate.
    # The shared test database may already contain unrelated local paths;
    # only rows owned by this host are expected to use its namespace.
    assert all(
        r["path"] is None or r["path"].startswith("remote:abc123:")
        for r in remote_rows
    )


@pytest.mark.asyncio
async def test_workspace_aggregate_keeps_local_rows_and_cached_rows_when_host_offline(
    monkeypatch,
):
    import json
    import backend.main as main_mod
    from backend.agent.config import save_config
    from backend.db.database import upsert_workspace

    local = await upsert_workspace("C:/local-project")
    save_config(
        {
            "remote_devices": [
                {
                    "host_id": "offline-host",
                    "url": "http://offline:8765",
                    "name": "Offline host",
                    "status": "offline",
                    "cached_workspaces": [
                        {
                            "id": 1,
                            "path": "remote:offline-host:/repo",
                            "label": "repo",
                            "last_opened_at": None,
                            "exists": True,
                            "conversation_count": 0,
                            "owner_id": "offline-host",
                            "device_status": "cached",
                        }
                    ],
                }
            ]
        }
    )
    async with await _client() as c:
        rows = (await c.get("/api/workspaces")).json()
        devices = (await c.get("/api/remote/devices")).json()["devices"]
    assert any(row["path"] == local["path"] and row["owner_id"] is None for row in rows)
    assert any(
        row["path"] == "remote:offline-host:/repo" and row["device_status"] == "cached"
        for row in rows
    )
    assert devices[0]["status"] == "offline"


@pytest.mark.asyncio
async def test_local_workspaces_view_hides_remote_rows():

    from backend.db.database import upsert_workspace

    await upsert_workspace("remote:deadbeef:/fake")
    async with await _client() as c:
        rows = (await c.get("/api/workspaces")).json()
        local = (await c.get("/api/workspaces/local")).json()
    assert all(remote_mod.parse_ns(r["path"]) is None for r in rows + local)


@pytest.mark.asyncio
async def test_files_proxy_routes_by_workspace_owner_not_active_host(monkeypatch):
    from backend.agent import remote as rm

    host_a = _StubSession(host_id="active-host")
    host_b = _StubSession(host_id="selected-host")
    remote_mod.register_remote(host_a, make_active=True)
    remote_mod.register_remote(host_b)
    _FakeProxyClient.response_body = {"root": "/host-b", "tree": []}
    monkeypatch.setattr(rm.httpx, "AsyncClient", _FakeProxyClient)
    async with await _client() as c:
        result = await c.get(
            "/api/files", params={"workspace": "remote:selected-host:/repo"}
        )
    assert result.status_code == 200
    assert _FakeProxyClient.last_request["url"].startswith(host_b.url)
    assert _FakeProxyClient.last_request["params"]["workspace"] == "/repo"


@pytest.mark.asyncio
async def test_files_proxy_rejects_foreign_host_namespace(monkeypatch):
    from backend.agent import remote as rm

    host = _StubSession()
    host.info["host_id"] = host.host_id = "abc123"
    remote_mod.register_remote(host, make_active=True)
    async with await _client() as c:
        res = await c.get("/api/files", params={"workspace": "remote:deadbeef:/x"})
    assert res.status_code == 400
    assert "different remote host" in res.json()["detail"]


# ---------------------------------------------------------------- lazy file tree


@pytest.mark.asyncio
async def test_file_tree_is_depth_limited_and_lazy(tmp_path):
    import os

    from backend.agent import tools as tools_mod

    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "a" / "b" / "deep.txt").write_text("x")
    (tmp_path / "top.txt").write_text("x")
    from httpx import ASGITransport, AsyncClient as _AC

    async with _AC(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get("/api/files", params={"workspace": str(tmp_path)})
    tree = {e["name"]: e for e in res.json()["tree"]}
    assert "top.txt" in tree
    # Level 2 dir is returned lazy: no children of a/b appear
    a = tree["a"]
    assert a["children"][0]["name"] == "b"
    assert all("children" not in k or not k["children"] for k in a["children"])
    assert a["children"][0].get("lazy") is True or a["children"][0]["type"] == "dir"

    # children endpoint returns one level, marking deeper dirs lazy
    async with _AC(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get(
            "/api/files/children",
            params={"workspace": str(tmp_path), "path": "a/b"},
        )
    entries = res.json()["entries"]
    assert {e["name"] for e in entries} == {"c", "deep.txt"}
    cdir = next(e for e in entries if e["name"] == "c")
    assert cdir.get("lazy") is True and "children" not in cdir

    # escape attempt refused
    async with _AC(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get(
            "/api/files/children",
            params={"workspace": str(tmp_path), "path": "../../etc"},
        )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_children_proxied_with_namespace(monkeypatch):
    from backend.agent import remote as rm

    host = _StubSession()
    host.info["host_id"] = host.host_id = "abc123"
    _FakeProxyClient.response_body = {"entries": []}
    monkeypatch.setattr(rm.httpx, "AsyncClient", _FakeProxyClient)
    remote_mod.set_remote(host)
    async with await _client() as c:
        res = await c.get(
            "/api/files/children",
            params={"workspace": "remote:abc123:sub", "path": "sub/dir"},
        )
    assert res.status_code == 200
    sent = _FakeProxyClient.last_request
    assert sent["params"]["workspace"] == "sub"  # namespace stripped
    assert sent["params"]["path"] == "sub/dir"


@pytest.mark.asyncio
async def test_connect_refuses_wrong_passphrase(monkeypatch):
    import backend.main as main_mod

    body = remote_mod.host_info()
    body["protocol"] = remote_mod.PROTOCOL_VERSION
    body["instance_id"] = "other"
    monkeypatch.setattr(main_mod.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.body = body
    _FakeAsyncClient.verify_status = 401
    async with await _client() as c:
        res = await c.post(
            "/api/remote/connect",
            json={"url": "http://10.0.0.5:8765", "passphrase": "stale"},
        )
    assert res.status_code == 401
    assert "wrong passphrase" in res.json()["detail"]
    assert remote_mod.get_remote() is None
    _FakeAsyncClient.verify_status = 200
