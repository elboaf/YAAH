"""Remote hosting: auth gate, host exec endpoint, client dispatch split,
handshake protocol check, discovery beacon properties."""
import pytest
from httpx import ASGITransport, AsyncClient

from backend.agent import remote as remote_mod
from backend.agent.config import load_config, save_config
from backend.main import app


@pytest.fixture(autouse=True)
def _no_remote_session():
    remote_mod.clear_remote()
    yield
    remote_mod.clear_remote()


def _set_host(passphrase: str):
    """Point the redirected test config's remote block at a host state."""
    save_config({"remote": {**load_config().get("remote", {}), "passphrase": passphrase}})


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


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


# ---------------------------------------------------------------- client dispatch

class _StubSession(remote_mod.RemoteSession):
    """Minimal RemoteSession stand-in: real proxy()/headers, stubbed exec."""

    def __init__(self, windows=False, result=None):
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
            },
        )
        self.result = result if result is not None else {"ok": True}
        self.calls = []

    async def exec_tool(self, name, args):
        self.calls.append((name, args))
        return self.result


@pytest.mark.asyncio
async def test_workspace_tools_route_to_remote(monkeypatch):
    stub = _StubSession(result={"exit_code": 0, "output": "remote!"})
    remote_mod.set_remote(stub)
    from backend.agent.tools import execute_tool

    result = await execute_tool("bash", {"command": "echo hi"}, "C:/local/ws")
    assert result == {"exit_code": 0, "output": "remote!"}
    assert stub.calls == [("bash", {"command": "echo hi"})]


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

    async def get(self, url):
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
            "/api/remote/connect", json={"url": "http://10.0.0.5:8765", "passphrase": "p"}
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
            "/api/remote/connect", json={"url": "http://10.0.0.5:8765", "passphrase": "p"}
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
            "/api/remote/connect", json={"url": "http://10.0.0.5:8765", "passphrase": "p"}
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
        return _FakeResponse(_FakeProxyClient.response_body, _FakeProxyClient.response_status)


@pytest.mark.asyncio
async def test_files_tree_proxies_to_host_when_connected(monkeypatch):
    from backend.agent import remote as rm

    _FakeProxyClient.response_body = {"root": "/home/host", "tree": []}
    monkeypatch.setattr(rm.httpx, "AsyncClient", _FakeProxyClient)
    remote_mod.set_remote(_StubSession())
    async with await _client() as c:
        res = await c.get("/api/files", params={"workspace": "C:/local"})
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
    remote_mod.set_remote(_StubSession())
    async with await _client() as c:
        res = await c.post(
            "/api/attachments",
            json={"workspace": "C:/local", "name": "a.txt", "content": "hi"},
        )
    assert res.json() == {"path": ".yaah-attachments/a.txt"}
    assert _FakeProxyClient.last_request["json"]["content"] == "hi"


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
