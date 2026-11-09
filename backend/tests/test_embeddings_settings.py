"""Embeddings config block + Settings endpoints (issue #677).

API-level tests over the real app: the embeddings block stays separate
from chat providers, the masked-key PUT merge never lets an echoed "set"
overwrite a stored key, api_base/model clear on blank (blank = keyword
search only), and the status/probe/rebuild/progress endpoints behave.
The embeddings endpoint is faked with a capturing transport (assertions
run against the actual request bodies).
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.agent import config as cfgmod
from backend.agent import embeddings
from backend.db import database


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Isolated config + DB; the real app under TestClient."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test_emb.db")
    monkeypatch.setattr(database, "_migrated", set())
    import backend.main as mainmod

    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    monkeypatch.setattr(mainmod, "save_config", cfgmod.save_config)
    # The rebuild progress summary is module state; a lingering summary
    # from an earlier test must not leak into this file's assertions.
    monkeypatch.setattr(mainmod, "_embeddings_rebuild_progress", {})
    from backend.main import app

    with TestClient(app) as c:
        yield c


class _CapturingTransport(httpx.MockTransport):
    def __init__(self, handler):
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return self.handler(request)


def _install_ok(monkeypatch, dim=4, model="bge-m3"):
    def handler(request):
        body = json.loads(request.content.decode("utf-8"))
        n = len(body["input"])
        return httpx.Response(200, content=json.dumps({
            "data": [{"embedding": [0.25] * dim} for _ in range(n)],
            "model": model,
        }).encode("utf-8"))

    transport = _CapturingTransport(handler)
    monkeypatch.setattr(embeddings, "_transport", transport)
    return transport


# ------------------------------------------------------------ config block

def test_embeddings_block_roundtrips_and_stays_out_of_providers(client):
    r = client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3", "api_key": "k"},
    })
    assert r.status_code == 200
    cfg = cfgmod.load_config()
    assert cfg["embeddings"]["api_base"] == "http://e:1"
    assert cfg["embeddings"]["model"] == "bge-m3"
    assert cfg["embeddings"]["api_key"] == "k"
    assert "bge-m3" not in cfg["providers"]  # never a chat provider entry


def test_masked_key_echo_never_overwrites_stored_key(client):
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3", "api_key": "real-key"},
    })
    # A Settings save that did not touch the key echoes "set".
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:2", "model": "bge-m3", "api_key": "set"},
    })
    assert cfgmod.load_config()["embeddings"]["api_key"] == "real-key"


def test_partial_save_keeps_sibling_keys(client):
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3", "api_key": "k"},
    })
    # A model-only save must not wipe the endpoint or the key.
    client.put("/api/config", json={"embeddings": {"model": "bge-new"}})
    block = cfgmod.load_config()["embeddings"]
    assert block["api_base"] == "http://e:1"
    assert block["model"] == "bge-new"
    assert block["api_key"] == "k"


def test_blank_endpoint_and_model_clear_the_block(client):
    """api_base/model are three-state: absent = untouched, blank = clear.
    Blank MUST win over the merge or the configured flag lies."""
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3", "api_key": "k"},
    })
    client.put("/api/config", json={"embeddings": {"api_base": "", "model": ""}})
    block = cfgmod.load_config()["embeddings"]
    assert block["api_base"] == ""
    assert block["model"] == ""
    assert block["api_key"] == "k"  # key survives an endpoint unconfigure


def test_get_view_masks_the_key(client):
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3", "api_key": "secret"},
    })
    view = client.get("/api/config").json()["embeddings"]
    assert view["api_key"] == "set"
    assert "secret" not in json.dumps(view)


# ------------------------------------------------------- status / probe

def test_status_reports_unconfigured_block(client):
    data = client.get("/api/embeddings/status").json()
    assert data["configured"] is False
    assert data["readiness"] == "not_configured"
    assert data["api_key"] == ""
    assert data["index"]["vectors"] == 0
    assert data["available"] is None  # never probed implicitly


def test_status_reports_ready_index(client):
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3"},
    })
    data = client.get("/api/embeddings/status").json()
    assert data["configured"] is True
    assert data["readiness"] == "no_index"  # configured but never built
    assert data["api_key"] == ""  # nothing stored -> masked to ""


def test_probe_reports_dimensions_and_model(client, monkeypatch):
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3"},
    })
    transport = _install_ok(monkeypatch, dim=1024)
    data = client.post("/api/embeddings/probe").json()
    assert data == {
        "configured": True, "available": True,
        "dimensions": 1024, "model": "bge-m3",
    }
    # The probe named the configured model on the wire.
    body = json.loads(transport.requests[0].content.decode("utf-8"))
    assert body["model"] == "bge-m3"


def test_probe_never_raises_when_unreachable(client, monkeypatch):
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3"},
    })

    def dead(request):
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setattr(embeddings, "_transport", _CapturingTransport(dead))
    data = client.post("/api/embeddings/probe").json()
    assert data["configured"] is True
    assert data["available"] is False
    assert "unreachable" in data["error"]


# ------------------------------------------------- rebuild + progress

def test_rebuild_builds_index_and_updates_status(client, monkeypatch):
    """One-click rebuild over the REAL vector store; only the endpoint
    is faked (capturing). Seeded messages get embedded and stored."""
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3"},
    })
    conv = client.post("/api/conversations", json={"title": "T", "workspace": "ws"}).json()
    client.post(f"/api/conversations/{conv['id']}/messages", json={
        "role": "user",
        "content": "The packaging stage failed during the deploy pipeline run.",
    })
    transport = _install_ok(monkeypatch, dim=4)

    r = client.post("/api/embeddings/rebuild")
    assert r.status_code == 200
    result = r.json()
    assert result["ok"] is True
    assert result["model"] == "bge-m3"
    assert result["dimensions"] == 4
    assert result["vectors_stored"] >= 1

    # Request bodies named the configured model; text went to the
    # configured endpoint (captured URLs only).
    for req in transport.requests:
        body = json.loads(req.content.decode("utf-8"))
        assert body["model"] == "bge-m3"
        assert str(req.url).startswith("http://e:1/")

    status = client.get("/api/embeddings/status").json()
    assert status["readiness"] == "ready"
    assert status["index"]["vectors"] >= 1
    assert status["index"]["model"] == "bge-m3"
    assert status["index"]["dimensions"] == 4


def test_rebuild_reports_error_when_endpoint_down(client, monkeypatch):
    """A dead endpoint degrades the BUILD (ok=False), it never 500s."""
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3"},
    })
    conv = client.post("/api/conversations", json={"title": "T", "workspace": "ws"}).json()
    client.post(f"/api/conversations/{conv['id']}/messages", json={
        "role": "user",
        "content": "The packaging stage failed during the deploy pipeline run.",
    })

    def dead(request):
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setattr(embeddings, "_transport", _CapturingTransport(dead))
    r = client.post("/api/embeddings/rebuild")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "unreachable" in r.json()["error"]
    # Nothing was recorded as a build (no dimensions -> no metadata).
    status = client.get("/api/embeddings/status").json()
    assert status["readiness"] == "no_index"


def test_rebuild_progress_lingers_then_resets(client, monkeypatch):
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3"},
    })
    _install_ok(monkeypatch, dim=4)
    assert client.get("/api/embeddings/rebuild/progress").json() == {}
    client.post("/api/embeddings/rebuild")
    prog = client.get("/api/embeddings/rebuild/progress").json()
    assert prog["running"] is False
    assert prog["phase"] == "done"
    assert "vectors stored" in prog["message"]
    # UI dismiss clears the lingering summary.
    r = client.post("/api/embeddings/rebuild/progress/reset")
    assert r.status_code == 200
    assert client.get("/api/embeddings/rebuild/progress").json() == {}


def test_rebuild_crash_marks_progress_finished(client, monkeypatch):
    """A build that RAISES must still flip progress to finished: the
    Settings card polls 'running' states forever otherwise (and hides
    the Dismiss button while doing it)."""
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3"},
    })
    import backend.main as mainmod
    from backend.agent import indexbuilder

    async def broken(progress_callback=None):
        raise RuntimeError("db gone mid-build")

    monkeypatch.setattr(indexbuilder, "build_index", broken)
    with pytest.raises(Exception):
        client.post("/api/embeddings/rebuild")
    prog = client.get("/api/embeddings/rebuild/progress").json()
    assert prog["running"] is False
    assert "rebuild failed" in prog["message"]
    assert mainmod._embeddings_rebuild_running is False


def test_failed_rebuild_leaves_no_stale_ready_metadata(client, monkeypatch):
    """Rebuild clears vectors AND metadata: a build that stores nothing
    reports no_index honestly instead of fake-ready."""
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3"},
    })
    conv = client.post("/api/conversations", json={"title": "T", "workspace": "ws"}).json()
    client.post(f"/api/conversations/{conv['id']}/messages", json={
        "role": "user",
        "content": "The packaging stage failed during the deploy pipeline run.",
    })
    # First build succeeds -> ready.
    _install_ok(monkeypatch, dim=4)
    assert client.post("/api/embeddings/rebuild").json()["ok"] is True
    status = client.get("/api/embeddings/status").json()
    assert status["readiness"] == "ready"

    # Rebuild with a now-dead endpoint: vectors were cleared, nothing stored.
    def dead(request):
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setattr(embeddings, "_transport", _CapturingTransport(dead))
    assert client.post("/api/embeddings/rebuild").json()["ok"] is False
    status = client.get("/api/embeddings/status").json()
    assert status["readiness"] == "no_index"
    assert status["index"]["vectors"] == 0



def test_default_workspace_scoped_search_finds_null_workspace_rows(client, monkeypatch):
    """Workspace-scoped semantic search must include Default-workspace
    conversations (workspace NULL at rest), not return an empty scope."""
    client.put("/api/config", json={
        "embeddings": {"api_base": "http://e:1", "model": "bge-m3"},
    })
    r = client.post("/api/conversations", json={"title": "T", "workspace": ""})
    assert r.status_code == 200
    conv_id = r.json()["id"]
    client.post(f"/api/conversations/{conv_id}/messages", json={
        "role": "user",
        "content": "The packaging stage failed during the deploy pipeline run.",
    })
    _install_ok(monkeypatch, dim=4)
    assert client.post("/api/embeddings/rebuild").json()["ok"] is True

    import asyncio
    from backend.agent.search_semantic import search_with_semantic
    from backend.db import database as dbmod

    async def scoped():
        async def keyword_fn(q, n):
            return await dbmod.search_all_workspaces(q, n)
        return await search_with_semantic(
            keyword_fn, "packaging", 10,
            workspace_reach="workspace", workspace="",
        )

    result = asyncio.new_event_loop().run_until_complete(scoped())
    assert result["semantic_state"] == "ready"
    assert result["semantic_matches"] >= 1


def test_fk_migration_clears_stale_build_metadata(tmp_path, monkeypatch):
    """The one-time FK migration drops the vectors AND the build metadata:
    readiness reads no_index afterwards, never fake-ready."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "mig.db")
    monkeypatch.setattr(database, "_migrated", set())
    import asyncio
    from backend.agent import vectorestore

    async def scenario():
        # First process: build a "ready" index (old JSON-vector schema is
        # the pre-migration shape only if the FKs are absent, so simulate
        # by building, then dropping the marker + FKs to force a rebuild).
        await database.create_conversation("M", "ws")
        await database.add_message(1, "user", "some transcript content here")
        await vectorestore.store_chunk_embeddings([{
            "conversation_id": 1, "message_id": 1, "role": "user",
            "chunk_text": "some transcript content here",
            "chunk_index": 0, "embedding": [0.1, 0.2, 0.3],
        }])
        await vectorestore.record_index_build("bge-m3", 3)
        assert vectorestore.semantic_readiness(
            "bge-m3", await vectorestore.get_index_metadata()) == "ready"
        # Simulate a pre-FK database: no FKs on the table, no marker.
        async with database.open_db() as db:
            await db.execute("DROP TABLE vector_index")
            await db.execute("""
                CREATE TABLE vector_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    conversation_id INTEGER NOT NULL,
                    role TEXT NOT NULL, chunk_text TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL, embedding BLOB NOT NULL,
                    UNIQUE(message_id, chunk_index)
                )
            """)
            await db.execute("DELETE FROM app_meta WHERE key = 'vector_index_fk_migrated'")
            await db.commit()
        # Second "process": the migration stamp is per-DB; force re-run.
        monkeypatch.setattr(database, "_migrated", set())
        async with database.open_db() as db:
            pass
        meta = await vectorestore.get_index_metadata()
        assert meta == {}
        assert vectorestore.semantic_readiness("bge-m3", meta) == "no_index"
        assert await vectorestore.get_vector_count() == 0

    asyncio.new_event_loop().run_until_complete(scenario())
