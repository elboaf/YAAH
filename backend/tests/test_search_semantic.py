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
from backend.agent import embeddings, vectorestore, modelserver
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


async def test_ready_semantic_ranking_added(monkeypatch):
    """Semantic leg runs, endpoint receives the configured model id."""
    await _seed()
    _configure()

    # Force the test to use the configured endpoint, not a live server.
    monkeypatch.setattr(
        "backend.agent.modelserver.get_effective_embeddings_endpoint",
        lambda: None,
    )

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


async def test_conversation_scope_contains_semantic_hits():
    """The single-conversation tool path passes [conversation_id]:
    semantic hits from OTHER conversations must not leak in ahead of
    the conversation's own keyword results."""
    await _seed()  # conversation 1
    await database.create_conversation("Other conversation", "ws")
    await database.add_message(
        2, "user",
        "Packaging topics live here too, in a different conversation.",
    )
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([
        {
            "conversation_id": 1, "message_id": 1, "role": "user",
            "chunk_text": "The packaging stage failed during the deploy pipeline run.",
            "chunk_index": 0, "embedding": [0.25] * 4,
        },
        {
            "conversation_id": 2, "message_id": 3, "role": "user",
            "chunk_text": "Packaging topics live here too.",
            "chunk_index": 0, "embedding": [0.25] * 4,  # identical vector: ties
        },
    ])
    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(
            _kw_fn, "packaging", 10, conversation_ids=[1])
    finally:
        embeddings._transport = None
    # Semantic hits carry conversation_id; the conversation-1 keyword
    # matches do not (single-conversation shape). Assert containment:
    # every hit WITH a conversation_id belongs to conversation 1, and no
    # message id from conversation 2 appears anywhere.
    for m in result["matches"]:
        if "conversation_id" in m:
            assert m["conversation_id"] == 1, f"foreign hit leaked in: {m}"
    ids = {m["message_id"] for m in result["matches"]}
    assert 3 not in ids, f"conversation-2 message leaked in: {ids}"


async def test_merged_hits_carry_classified_shape():
    """Merged results have ONE contract shape: every match carries kind,
    and the T3 identity fields when the endpoint caller needs them."""
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
    assert result["semantic_state"] == "ready"
    # Semantic hits carry the classified identity fields; single-
    # conversation keyword matches keep their minimal T2 shape.
    for m in result["matches"]:
        assert m["kind"] in ("chat", "tool", "sub-agent")
    for m in result["matches"]:
        if m.get("semantic") or "similarity" in m:
            assert "conversation_id" in m


async def test_global_search_keeps_none_scope():
    """No scope args = global (the HTTP search): other conversations'
    semantic hits DO surface, ranked first."""
    await _seed()  # conversation 1
    await database.create_conversation("Other conversation", "ws")
    await database.add_message(
        2, "user",
        "Packaging topics live here too, in a different conversation.",
    )
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 2, "message_id": 3, "role": "user",
        "chunk_text": "Packaging topics live here too.",
        "chunk_index": 0, "embedding": [0.25] * 4,
    }])
    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None
    # The other conversation's semantic hit surfaced globally: it appears
    # with conversation identity (or at least its message id is present).
    ids = {m["message_id"] for m in result["matches"]}
    assert 3 in ids, f"global scope lost the foreign semantic hit: {ids}"
    assert result["semantic_matches"] >= 1


async def test_tool_scopes_semantic_hits_to_conversation():
    """Regression (review round): the REAL tool path -- tools.
    search_conversation_history -- must scope its semantic half to the
    conversation. Without the pass-through, hits from other conversations
    leak in ahead of the conversation's own keyword results."""
    from backend.agent.tools import search_conversation_history
    await _seed()  # conversation 1
    await database.create_conversation("Other conversation", "ws")
    await database.add_message(
        2, "user",
        "Packaging topics live here too, in a different conversation.",
    )
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 2, "message_id": 3, "role": "user",
        "chunk_text": "Packaging topics live here too.",
        "chunk_index": 0, "embedding": [0.25] * 4,
    }])
    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_conversation_history(
            workspace="ws", conversation_id=1, query="packaging", max_results=10
        )
    finally:
        embeddings._transport = None
    ids = {m["message_id"] for m in result["matches"]}
    assert 3 not in ids, f"tool leaked a foreign conversation hit: {ids}"


async def test_workspace_scope_includes_related_workspaces():
    """workspace reach = the workspace PLUS its related workspaces -- the
    same coverage the keyword search walks (review round 2)."""
    await _seed()  # conversation 1, workspace "ws"
    await database.create_conversation("Related ws chat", "ws-related")
    await database.add_message(
        2, "user",
        "Packaging topics live in a related workspace conversation.",
    )
    await database.set_related_workspaces("ws", ["ws-related"])
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 2, "message_id": 3, "role": "user",
        "chunk_text": "Packaging topics live in a related workspace conversation.",
        "chunk_index": 0, "embedding": [0.25] * 4,
    }])
    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(
            _kw_fn, "packaging", 10,
            workspace_reach="workspace", workspace="ws",
        )
    finally:
        embeddings._transport = None
    ids = {m["message_id"] for m in result["matches"]}
    assert 3 in ids, f"related-workspace hit missing from workspace scope: {ids}"
    assert result["semantic_matches"] >= 1


# ------------------------------------------------------------ reranking (T7c)


async def test_reranker_state_unavailable_when_no_endpoint(monkeypatch):
    """No reranker endpoint → reranker_state 'unavailable', RRF order kept."""
    await _seed()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 1, "message_id": 1, "role": "user",
        "chunk_text": "packaging content", "chunk_index": 0,
        "embedding": [0.25] * 4,
    }])
    monkeypatch.setattr(modelserver, "get_effective_reranker_endpoint", lambda: None)
    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None
    assert result["reranker_state"] == "unavailable"
    assert result["semantic_state"] == "ready"


async def test_reranker_state_applied(monkeypatch):
    """Reranker endpoint up + scores returned → reranker_state 'applied'."""
    await _seed()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 1, "message_id": 1, "role": "user",
        "chunk_text": "packaging content alpha", "chunk_index": 0,
        "embedding": [0.25] * 4,
    }])
    # Fake the reranker endpoint resolution AND the rerank call
    monkeypatch.setattr(modelserver, "get_effective_reranker_endpoint",
                        lambda: "http://localhost:9999/v1")
    from backend.agent import reranker as reranker_mod
    monkeypatch.setattr(reranker_mod, "rerank",
                        _fake_rerank_reverses)
    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None
    assert result["reranker_state"] == "applied"


async def _fake_rerank_reverses(query, documents, endpoint=None):
    """Fake reranker that reverses order and attaches scores."""
    out = list(reversed(documents))
    for i, d in enumerate(out):
        d["rerank_score"] = 1.0 - i * 0.1
    return out


async def test_reranker_fail_open_on_error(monkeypatch):
    """Reranker raising → RRF order kept, reranker_state 'unavailable'."""
    await _seed()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 1, "message_id": 1, "role": "user",
        "chunk_text": "packaging content", "chunk_index": 0,
        "embedding": [0.25] * 4,
    }])
    monkeypatch.setattr(modelserver, "get_effective_reranker_endpoint",
                        lambda: "http://localhost:9999/v1")
    from backend.agent import reranker as reranker_mod

    async def _boom(query, documents, endpoint=None):
        raise RuntimeError("reranker exploded")
    monkeypatch.setattr(reranker_mod, "rerank", _boom)
    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None
    assert result["reranker_state"] == "unavailable"
    assert result["semantic_state"] == "ready"
    assert result["count"] >= 1
