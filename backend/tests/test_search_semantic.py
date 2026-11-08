"""Semantic search integration tests (issue #677, Phase 2a).

Degradation ladder through the REAL keyword path (FTS5) and the REAL
vector store, with only the embeddings endpoint faked (capturing).
Proves: keyword contract preserved, semantic ranking on top, and every
failure mode lands in keyword results + semantic_unavailable.
"""
import json

import httpx
import pytest

from backend.agent import config as config_mod
from backend.agent import embeddings, vectorestore
from backend.agent.search_semantic import search_with_semantic
from backend.db import database

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test_sem.db")
    monkeypatch.setattr(database, "_migrated", set())
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")


class _CapturingTransport(httpx.MockTransport):
    def __init__(self, handler):
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return self.handler(request)


def _configure(model="bge-m3"):
    config_mod.save_config({"embeddings": {
        "api_base": "http://x", "api_key": "", "model": model,
    }})


def _respond(vec=None, model="bge-m3", dim=4):
    v = vec or [0.25] * dim

    def handler(request):
        body = json.loads(request.content.decode("utf-8"))
        n = len(body["input"])
        return httpx.Response(200, content=json.dumps({
            "data": [{"embedding": v} for _ in range(n)],
            "model": model,
        }).encode("utf-8"))

    return _CapturingTransport(handler)


async def _seed(conversation_id=1):
    await database.create_conversation("Semantic test", "ws")
    await database.add_message(
        conversation_id, "user",
        "The packaging stage failed during the deploy pipeline run.",
    )
    await database.add_message(
        conversation_id, "assistant",
        "Deployment pipelines fail at packaging when artifacts mismatch.",
    )


# ------------------------------------------------------------ degradation

async def test_not_configured_keyword_only():
    await _seed()
    result = await search_with_semantic(
        lambda q, n: _kw(q, n), "packaging failed", 10)
    assert result["semantic_state"] == "not_configured"
    assert result["count"] >= 1
    assert "semantic_unavailable" not in result


def _kw(query, max_results):
    """The real FTS keyword search as keyword_fn."""
    from backend.db.database import search_conversation_history
    conv_marker = {"id": 1}

    async def run(q, n):
        return await database.search_conversation_history(1, q, n)
    # search_with_semantic expects either sync dict or awaitable;
    # keyword_fn may be async -- wrap.
    return run(query, max_results)


async def test_ready_semantic_ranking_added():
    await _seed()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 1, "message_id": 1, "role": "user",
        "chunk_text": "The packaging stage failed during the deploy pipeline run.",
        "chunk_index": 0,
        "embedding": [0.25, 0.25, 0.25, 0.25],
    }])
    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None
    assert result["semantic_state"] == "ready"
    assert result["semantic_matches"] >= 1
    # Query embedding went to the configured endpoint, naming the model.
    assert len(transport.requests) == 1
    body = json.loads(transport.requests[0].content.decode("utf-8"))
    assert body["model"] == "bge-m3"
    # Semantic hit leads with its similarity score.
    top = result["matches"][0]
    assert "similarity" in top


async def test_endpoint_unreachable_degrades_to_keyword():
    await _seed()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 1, "message_id": 1, "role": "user",
        "chunk_text": "packaging content", "chunk_index": 0,
        "embedding": [0.25] * 4,
    }])

    def dead(request):
        raise httpx.ConnectError("down", request=request)

    transport = _CapturingTransport(dead)
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None

    # Keyword still works, semantic reports unavailable.
    assert result["count"] >= 1
    assert "packaging" in result["matches"][0]["excerpt"].lower()
    assert result["semantic_state"] == "unavailable"
    assert "unreachable" in result["semantic_unavailable"]


async def test_model_mismatch_degrades_and_advises_rebuild():
    await _seed()
    _configure(model="bge-new")
    await vectorestore.record_index_build("bge-old", 4)
    transport = _respond(model="bge-old")
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None
    assert result["semantic_state"] == "unavailable"
    assert "rebuild" in result["semantic_unavailable"]
    # No query embed was even attempted (mismatch short-circuits first).
    assert transport.requests == []
    assert result["count"] >= 1  # keyword unaffected


async def test_no_index_degrades():
    await _seed()
    _configure()  # configured, never built
    result = await search_with_semantic(_kw_fn, "packaging", 10)
    assert result["semantic_state"] == "unavailable"
    assert "not built" in result["semantic_unavailable"]


async def test_dimension_drift_degrades():
    """Index built at 4 dims, endpoint now returns 8: refuse to score."""
    await _seed()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    transport = _respond(dim=8)
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None
    assert result["semantic_state"] == "unavailable"
    assert "rebuild" in result["semantic_unavailable"]
    assert result["count"] >= 1


async def test_vector_store_fault_degrades():
    await _seed()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)

    async def broken_scan(*a, **k):
        raise RuntimeError("db gone")

    import backend.agent.search_semantic as ss
    original = ss.vectorestore.semantic_search
    ss.vectorestore.semantic_search = broken_scan
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        ss.vectorestore.semantic_search = original
    assert result["semantic_state"] == "unavailable"
    assert result["count"] >= 1


# ------------------------------------------------------------ shape

async def test_keyword_contract_shape_preserved():
    """No embeddings config at all: the response is exactly the FTS
    shape plus semantic_state -- callers that ignore the field are
    unaffected."""
    await _seed()
    result = await search_with_semantic(_kw_fn, "packaging", 10)
    for m in result["matches"]:
        assert set(m) == {"message_id", "role", "field", "excerpt"}
    assert result["count"] == len(result["matches"])
    assert isinstance(result["truncated"], bool)


async def test_merge_dedupes_by_message_id():
    """A message both keyword-matched and semantically scored appears
    once, in the semantic slot."""
    await _seed()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 1, "message_id": 1, "role": "user",
        "chunk_text": "The packaging stage failed during the deploy pipeline run.",
        "chunk_index": 0, "embedding": [0.25] * 4,
    }])
    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None
    ids = [m["message_id"] for m in result["matches"]]
    assert len(ids) == len(set(ids))


async def test_semantic_zero_hits_reports_ready_zero():
    """Ready index, but nothing scores above the floor: keyword results
    pass through untouched and state stays ready."""
    await _seed()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 1, "message_id": 1, "role": "user",
        "chunk_text": "unrelated almanac entries about weather balloons",
        "chunk_index": 0, "embedding": [0.25] * 4,
    }])
    # Orthogonal query vector -> similarity 0 -> below floor.
    def ortho(request):
        body = json.loads(request.content.decode("utf-8"))
        n = len(body["input"])
        return httpx.Response(200, content=json.dumps({
            "data": [{"embedding": [-1.0, 1.0, -1.0, 1.0]} for _ in range(n)],
            "model": "bge-m3",
        }).encode("utf-8"))

    transport = _CapturingTransport(ortho)
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None
    assert result["semantic_state"] == "ready"
    assert result["semantic_matches"] == 0
    assert result["count"] >= 1


async def test_http_endpoint_shape(tmp_path, monkeypatch):
    """api_search returns the semantic-layer shape over the real app."""
    from fastapi.testclient import TestClient
    await _seed()
    import backend.main as main_mod
    client = TestClient(main_mod.app)
    r = client.get("/api/search", params={"query": "packaging failed"})
    assert r.status_code == 200
    data = r.json()
    assert data["semantic_state"] == "not_configured"
    assert data["count"] >= 1
    r2 = client.get("/api/search", params={"query": ""})
    assert r2.status_code == 400


async def test_embeddings_status_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    import backend.main as main_mod
    client = TestClient(main_mod.app)
    r = client.get("/api/embeddings/status")
    assert r.status_code == 200
    data = r.json()
    assert data["configured"] is False
    assert data["readiness"] == "not_configured"
    assert data["index"]["vectors"] == 0


async def test_rebuild_endpoint_runs_and_reports(tmp_path):
    from fastapi.testclient import TestClient
    import backend.main as main_mod
    _configure()
    await _seed()
    transport = _respond()
    embeddings._transport = transport
    client = TestClient(main_mod.app)
    try:
        r = client.post("/api/embeddings/rebuild")
    finally:
        embeddings._transport = None
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["vectors_stored"] >= 1
    assert data["model"] == "bge-m3"
    # Status now reports a built index.
    status = client.get("/api/embeddings/status").json()
    assert status["readiness"] == "ready"
    assert status["index"]["vectors"] >= 1
    assert status["index"]["last_build"]


def _kw_fn(query, max_results):
    """Async keyword_fn over the real FTS search for conversation 1."""
    async def run(q, n):
        return await database.search_conversation_history(1, q, n)
    return run(query, max_results)
