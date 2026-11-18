"""Task-scoped server routes (#760): the reranking tenant.

The multi-tenant registry (ADR-0022) always supported a reranking
ServerManager, but every /api/server/* route hardcoded the embedding
tenant -- so the reranker could never be started and the Settings model
picker could misroute a reranker into the embedding slot. These tests
pin the task parameter surface: status/start/stop/download/gpu-pref
routes accept "task", validate it, and never let the tenants' state
bleed into each other. All process work is mocked; nothing spawns.
"""

import pytest
from fastapi.testclient import TestClient


class _FakeManager:
    """Stand-in for ServerManager: records stop calls, no real process."""

    def __init__(self, running=False, port=None):
        self.port = port
        self.stop_calls = 0
        self.start_calls = []

    def is_running(self):
        return self.port is not None

    async def stop(self):
        self.stop_calls += 1

    async def start(self, model_id, *a, **kw):
        self.start_calls.append(model_id)
        return 58999


@pytest.fixture
def client(monkeypatch, tmp_path):
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    import backend.main as mainmod

    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    from backend.main import app

    with TestClient(app) as c:
        yield c, mainmod


def _no_gpus(monkeypatch, mainmod):
    monkeypatch.setattr(
        mainmod.modelserver, "enumerate_gpus", lambda *a, **kw: []
    )


def _patch_tenant(monkeypatch, mainmod, fake, tasks=("embedding", "reranking")):
    """Swap the tenant registry for a fake manager.

    get_tenant_lock's internals call the module-global get_tenant, so the
    lock must be patched in lockstep or the registry dicts diverge
    (KeyError on the lock lookup).
    """
    import asyncio

    monkeypatch.setattr(mainmod.modelserver, "get_tenant", lambda task: fake)
    monkeypatch.setattr(
        mainmod.modelserver, "get_tenant_lock", lambda task: asyncio.Lock()
    )


def test_status_reports_task_and_defaults_to_embedding(client, monkeypatch):
    c, mainmod = client
    _no_gpus(monkeypatch, mainmod)
    monkeypatch.setattr(
        mainmod.modelserver,
        "get_server_config",
        lambda task="embedding": ("", "qwen3-embedding-0.6b"),
    )
    body = c.get("/api/server/status").json()
    assert body["task"] == "embedding"
    assert body["model"] == "qwen3-embedding-0.6b"

    body = c.get("/api/server/status?task=reranking").json()
    assert body["task"] == "reranking"
    assert body["running"] is False


def test_status_rejects_unknown_task(client):
    c, _ = client
    body = c.get("/api/server/status?task=bogus").json()
    assert body["ok"] is False
    assert "reranking" in body["error"]


def test_status_running_reranker_reports_its_own_port(client, monkeypatch):
    c, mainmod = client
    _no_gpus(monkeypatch, mainmod)
    monkeypatch.setattr(
        mainmod.modelserver,
        "get_server_config",
        lambda task="embedding": ("bin", "qwen3-reranker-0.6b"),
    )
    fake = _FakeManager(port=58915)
    monkeypatch.setattr(mainmod.modelserver, "get_tenant", lambda task: fake)
    body = c.get("/api/server/status?task=reranking").json()
    assert body["running"] is True
    assert body["port"] == 58915
    assert body["endpoint"] == "http://localhost:58915/v1"
    assert body["model"] == "qwen3-reranker-0.6b"


def test_start_reranking_tenant(client, monkeypatch):
    c, mainmod = client
    monkeypatch.setattr(
        mainmod.modelserver, "ensure_server_binary", lambda *a, **kw: "installed"
    )
    monkeypatch.setattr(
        mainmod.modelserver,
        "get_server_config",
        lambda task="embedding": ("installed", "qwen3-reranker-0.6b"),
    )
    fake = _FakeManager()
    _patch_tenant(monkeypatch, mainmod, fake)
    monkeypatch.setattr(
        mainmod.modelserver, "is_model_downloaded", lambda entry: True
    )
    monkeypatch.setattr(
        mainmod.modelserver,
        "download_model",
        lambda entry, progress_cb=None: entry,
    )
    body = c.post(
        "/api/server/start", json={"task": "reranking"}
    ).json()
    assert body["ok"] is True, body
    # default_model("reranking") resolved through the manifest.
    assert fake.start_calls == ["qwen3-reranker-0.6b"]


def test_start_rejects_unknown_task_before_any_spawn(client):
    c, _ = client
    body = c.post("/api/server/start", json={"task": "bogus"}).json()
    assert body["ok"] is False
    assert "unknown task" in body["error"]


def test_start_running_tenant_is_idempotent(client, monkeypatch):
    c, mainmod = client
    fake = _FakeManager(port=58916)
    _patch_tenant(monkeypatch, mainmod, fake)
    # ensure_server_binary runs BEFORE the running check in the route:
    # stub it so the test never touches the network or the real install
    # location on hosts without a pinned llama-server (CodeRabbit #760).
    monkeypatch.setattr(
        mainmod.modelserver, "ensure_server_binary", lambda *a, **kw: "stubbed"
    )
    body = c.post(
        "/api/server/start", json={"task": "reranking"}
    ).json()
    assert body == {"ok": True, "port": 58916}
    assert fake.start_calls == []


def test_stop_stops_only_the_named_tenant(client, monkeypatch):
    c, mainmod = client
    fakes = {"embedding": _FakeManager(port=1), "reranking": _FakeManager(port=2)}
    monkeypatch.setattr(mainmod.modelserver, "get_tenant", lambda task: fakes[task])
    body = c.post("/api/server/stop", json={"task": "reranking"}).json()
    assert body["ok"] is True
    assert fakes["reranking"].stop_calls == 1
    assert fakes["embedding"].stop_calls == 0


def test_gpu_offload_is_per_tenant(client, monkeypatch):
    c, mainmod = client
    seen = {}
    monkeypatch.setattr(
        mainmod.modelserver,
        "set_server_gpu_offload",
        lambda value, task="embedding": seen.setdefault("args", (value, task)),
    )
    body = c.post(
        "/api/server/gpu-offload",
        json={"gpu_offload": True, "task": "reranking"},
    ).json()
    assert body["ok"] is True
    assert seen["args"] == (True, "reranking")


def test_gpu_index_is_per_tenant(client, monkeypatch):
    c, mainmod = client
    seen = {}
    monkeypatch.setattr(
        mainmod.modelserver,
        "set_server_gpu_index",
        lambda value, task="embedding": seen.setdefault("args", (value, task)),
    )
    body = c.post(
        "/api/server/gpu-index",
        json={"gpu_index": 1, "task": "reranking"},
    ).json()
    assert body["ok"] is True
    assert body["gpu_index"] == 1
    assert seen["args"] == (1, "reranking")


def test_download_defaults_to_the_task_model(client, monkeypatch):
    c, mainmod = client
    monkeypatch.setattr(
        mainmod.modelserver,
        "find_model",
        lambda model_id: {"id": model_id, "files": ["x.gguf"]},
    )
    monkeypatch.setattr(
        mainmod.modelserver, "is_model_downloaded", lambda entry: True
    )
    body = c.post("/api/server/download", json={"task": "reranking"}).json()
    assert body == {"ok": True, "message": "already downloaded"}


def test_lifespan_stops_every_tenant_on_shutdown(client, monkeypatch):
    """The boot shutdown must not orphan a started reranker (#760)."""
    c, mainmod = client
    fakes = {"embedding": _FakeManager(port=1), "reranking": _FakeManager(port=2)}
    monkeypatch.setattr(mainmod.modelserver, "get_tenant", lambda task: fakes[task])
    with TestClient(c.app):
        pass
    # TestClient shutdown ran the lifespan tail: both tenants stopped.
    assert fakes["embedding"].stop_calls >= 1
    assert fakes["reranking"].stop_calls >= 1

def test_start_rejects_model_from_wrong_task(client, monkeypatch):
    """The API twin of the dropdown misroute (#760 CodeRabbit Major):
    task=reranking with an embedding-only model must be refused before
    any spawn — it would run --embeddings flags under the reranking
    tenant (and vice versa)."""
    c, mainmod = client
    fake = _FakeManager()
    _patch_tenant(monkeypatch, mainmod, fake)
    monkeypatch.setattr(
        mainmod.modelserver, "ensure_server_binary", lambda *a, **kw: "stubbed"
    )
    monkeypatch.setattr(
        mainmod.modelserver,
        "get_server_config",
        lambda task="embedding": ("stubbed", ""),
    )
    body = c.post(
        "/api/server/start",
        json={"task": "reranking", "model": "qwen3-embedding-0.6b"},
    ).json()
    assert body["ok"] is False, body
    assert "does not support task" in body["error"]
    assert fake.start_calls == []


def test_stop_rejects_falsy_task_instead_of_defaulting(client, monkeypatch):
    """Strict task validation (#760 CodeRabbit): {"task": false} / "" /
    0 / [] must be rejected, never defaulted to the embedding tenant —
    a default here would stop a running server the caller never named."""
    c, mainmod = client
    fakes = {"embedding": _FakeManager(port=1), "reranking": _FakeManager(port=2)}
    monkeypatch.setattr(mainmod.modelserver, "get_tenant", lambda task: fakes[task])
    for bad in (False, "", 0, []):
        body = c.post("/api/server/stop", json={"task": bad}).json()
        assert body["ok"] is False, (bad, body)
        assert "unknown task" in body["error"]
    assert all(f.stop_calls == 0 for f in fakes.values())
