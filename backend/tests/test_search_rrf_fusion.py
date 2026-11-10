"""RRF fusion integration tests (issue #725, Phase 3).

Tests the reciprocal rank fusion of keyword and semantic legs per ADR-0023.
Covers: fusion ordering + tie-break chain, chunk aggregation/dedupe, kw:
opt-out, redefined count semantics, sources array, degrade pass-through,
assembled-scope fusion, endpoint shape.
"""
import json
import math

import httpx
import pytest

from backend.agent import config as config_mod
from backend.agent import embeddings, vectorestore
from backend.agent.search_semantic import (
    rrf_score, RRF_K, FUSION_DEPTH,
    search_with_semantic,
)
from backend.db import database


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test_rrf.db")
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


async def _seed_messages():
    """Seed multiple conversations with distinct content."""
    # Conversation 1: packaging
    await database.create_conversation("Packaging discussion", "ws")
    await database.add_message(
        1, "user", "The packaging stage failed during the deploy pipeline run."
    )
    await database.add_message(
        1, "assistant", "Deployment pipelines fail at packaging when artifacts mismatch."
    )

    # Conversation 2: deployment
    await database.create_conversation("Deployment config", "ws")
    await database.add_message(
        2, "user", "We need to configure the deployment targets for production."
    )
    await database.add_message(
        2, "assistant", "Production deployment targets require careful configuration."
    )

    # Conversation 3: testing
    await database.create_conversation("Test suite", "ws")
    await database.add_message(
        3, "user", "The automated test suite is passing on the staging branch."
    )


async def _kw_fn(query, max_results):
    """Async keyword_fn over the real FTS search for conversation 1."""
    return await database.search_conversation_history(1, query, max_results)


async def _global_kw_fn(query, max_results):
    """Async keyword_fn over all workspaces."""
    return await database.search_all_workspaces(query, max_results)


# ============================================================
# Unit tests for RRF scoring
# ============================================================

def test_rrf_score_decreases_with_rank():
    assert rrf_score(1) > rrf_score(2) > rrf_score(10)
    assert rrf_score(1) == 1.0 / (RRF_K + 1)


def test_rrf_k_constant():
    assert RRF_K == 60


def test_fusion_depth_constant():
    assert FUSION_DEPTH == 100


# ============================================================
# Fusion ordering
# ============================================================

@pytest.mark.asyncio
async def test_fusion_ranks_cross_leg_agreement_high():
    """A message ranked high in BOTH legs should fuse to the top."""
    await _seed_messages()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)

    # Index conversation 1 message 1 (packaging) with a strong semantic signal
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 1, "message_id": 1, "role": "user",
        "chunk_text": "The packaging stage failed during the deploy pipeline run.",
        "chunk_index": 0, "embedding": [0.25, 0.25, 0.25, 0.25],
    }])

    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None

    assert result["semantic_state"] == "ready"
    # Message 1 is keyword rank 1 AND semantic rank 1 → fused top
    assert result["matches"][0]["message_id"] == 1
    # It should have both sources
    assert result["matches"][0]["sources"] == ["keyword", "semantic"]


@pytest.mark.asyncio
async def test_fusion_tie_break_keyword_rank():
    """Exact-tie ordering: equal fused score + equal keyword rank falls
    through to message_id ascending."""
    await database.create_conversation("Tie break test", "ws")
    # Same exact text for both so FTS ranks them equally (kw ranks equal)
    await database.add_message(
        1, "user", "Identical search term content"
    )
    await database.add_message(
        1, "user", "Identical search term content"
    )

    _configure()
    await vectorestore.record_index_build("bge-m3", 4)

    # Identical semantic vectors → identical similarity → tied semantic
    # contribution, so both messages fuse to the SAME score.
    await vectorestore.store_chunk_embeddings([
        {
            "conversation_id": 1, "message_id": 1, "role": "user",
            "chunk_text": "Identical search term content",
            "chunk_index": 0,
            "embedding": [0.25, 0.25, 0.25, 0.25],
        },
        {
            "conversation_id": 1, "message_id": 2, "role": "user",
            "chunk_text": "Identical search term content",
            "chunk_index": 0,
            "embedding": [0.25, 0.25, 0.25, 0.25],
        },
    ])

    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "search term content", 10)
    finally:
        embeddings._transport = None

    assert result["semantic_state"] == "ready"
    ids = [m["message_id"] for m in result["matches"]]
    # Unconditional: both must be present, first two exactly [1, 2]
    assert len(ids) >= 2, f"expected both tied messages, got {ids}"
    assert ids[:2] == [1, 2]
    assert len(ids) == len(set(ids))  # no duplicates


@pytest.mark.asyncio
async def test_fusion_deterministic_ordering():
    """Same inputs → same output order every run."""
    await _seed_messages()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 1, "message_id": 1, "role": "user",
        "chunk_text": "The packaging stage failed during the deploy pipeline run.",
        "chunk_index": 0, "embedding": [0.25, 0.25, 0.25, 0.25],
    }])

    transport = _respond()
    embeddings._transport = transport
    try:
        r1 = await search_with_semantic(_kw_fn, "packaging", 10)
        r2 = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None

    ids1 = [m["message_id"] for m in r1["matches"]]
    ids2 = [m["message_id"] for m in r2["matches"]]
    assert ids1 == ids2


# ============================================================
# Chunk aggregation / dedupe
# ============================================================

@pytest.mark.asyncio
async def test_aggregation_best_chunk_per_message():
    """Multiple chunks per message → best chunk wins for that message."""
    await database.create_conversation("Multi-chunk test", "ws")
    await database.add_message(
        1, "user", "First chunk text about the topic"
    )
    await database.add_message(
        1, "user", "Second chunk also about the same topic"
    )

    _configure()
    await vectorestore.record_index_build("bge-m3", 4)

    # Two chunks for same message, different quality
    await vectorestore.store_chunk_embeddings([
        {
            "conversation_id": 1, "message_id": 1, "role": "user",
            "chunk_text": "First chunk text about the topic",
            "chunk_index": 0,
            "embedding": [0.1, 0.1, 0.1, 0.1],
        },
        {
            "conversation_id": 1, "message_id": 1, "role": "user",
            "chunk_text": "Second chunk also about the same topic",
            "chunk_index": 1,
            "embedding": [0.25, 0.25, 0.25, 0.25],  # better similarity
        },
    ])

    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "chunk topic", 10)
    finally:
        embeddings._transport = None

    # Message 1 should appear exactly once (present and deduped)
    ids = [m["message_id"] for m in result["matches"]]
    assert 1 in ids
    assert ids.count(1) == 1


# ============================================================
# kw: opt-out
# ============================================================

@pytest.mark.asyncio
async def test_kw_prefix_opt_out_keyword_only():
    """kw: prefix forces keyword-only, emits semantic_state 'off'."""
    await _seed_messages()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 1, "message_id": 1, "role": "user",
        "chunk_text": "The packaging stage failed during the deploy pipeline run.",
        "chunk_index": 0, "embedding": [0.25, 0.25, 0.25, 0.25],
    }])

    # Semantic is ready but kw: should bypass it
    result = await search_with_semantic(_kw_fn, "kw: packaging", 10)

    assert result["semantic_state"] == "off"
    assert result["semantic_matches"] == 0
    # Keyword results still present
    assert result["count"] >= 1
    # No sources field (or only keyword) on hits
    for m in result["matches"]:
        assert m.get("sources") is None or m.get("sources") == ["keyword"]


@pytest.mark.asyncio
async def test_kw_prefix_stripped_before_search():
    """The kw: prefix is removed from the query before searching."""
    await _seed_messages()

    result = await search_with_semantic(_kw_fn, "kw:packaging failed", 10)

    # Should still match "packaging failed" content
    assert result["count"] >= 1
    assert any("packaging" in m["excerpt"].lower() for m in result["matches"])


# ============================================================
# Redefined count semantics
# ============================================================

@pytest.mark.asyncio
async def test_semantic_matches_counts_fused_hits_with_semantic():
    """semantic_matches = number of fused hits with semantic contribution."""
    await _seed_messages()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 1, "message_id": 1, "role": "user",
        "chunk_text": "The packaging stage failed during the deploy pipeline run.",
        "chunk_index": 0, "embedding": [0.25, 0.25, 0.25, 0.25],
    }])

    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None

    assert result["semantic_state"] == "ready"
    # semantic_matches must reflect at least one real semantic contribution
    assert result["semantic_matches"] >= 1
    # ...and equal the number of hits carrying a semantic source
    actual_semantic = sum(
        1 for m in result["matches"]
        if "semantic" in m.get("sources", [])
    )
    assert result["semantic_matches"] == actual_semantic


@pytest.mark.asyncio
async def test_truncated_reflects_fused_candidates():
    """truncated = true when fused candidates exceed max_results."""
    await _seed_messages()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)

    # Index multiple messages to create more candidates than max_results
    for mid in range(1, 6):
        if mid <= 2:
            conv_id = 1
        elif mid <= 4:
            conv_id = 2
        else:
            conv_id = 3
        await vectorestore.store_chunk_embeddings([{
            "conversation_id": conv_id, "message_id": mid, "role": "user",
            "chunk_text": f"Content for message {mid}",
            "chunk_index": 0, "embedding": [0.25, 0.25, 0.25, 0.25],
        }])

    transport = _respond()
    embeddings._transport = transport
    try:
        # Small max_results to force truncation
        result = await search_with_semantic(_global_kw_fn, "content", 2)
    finally:
        embeddings._transport = None

    assert result["semantic_state"] == "ready"
    # Unconditional: 5 indexed messages + keyword hits at max_results=2
    # means candidates exceed the cut.
    assert result["count"] == 2
    assert result["truncated"] is True


# ============================================================
# Sources array
# ============================================================

@pytest.mark.asyncio
async def test_sources_array_indicates_contributing_legs():
    """Each hit has sources array indicating which legs contributed."""
    await _seed_messages()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 1, "message_id": 1, "role": "user",
        "chunk_text": "The packaging stage failed during the deploy pipeline run.",
        "chunk_index": 0, "embedding": [0.25, 0.25, 0.25, 0.25],
    }])

    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None

    assert result["semantic_state"] == "ready"
    for m in result["matches"]:
        assert "sources" in m
        assert isinstance(m["sources"], list)
        for src in m["sources"]:
            assert src in ("keyword", "semantic")


# ============================================================
# Degrade pass-through
# ============================================================

@pytest.mark.asyncio
async def test_degrade_not_configured():
    """Not configured → keyword-only, state not_configured."""
    await _seed_messages()

    result = await search_with_semantic(_kw_fn, "packaging", 10)

    assert result["semantic_state"] == "not_configured"
    assert result["count"] >= 1


@pytest.mark.asyncio
async def test_degrade_no_index():
    """Configured but no index → state no_index."""
    await _seed_messages()
    _configure()

    result = await search_with_semantic(_kw_fn, "packaging", 10)

    assert result["semantic_state"] == "unavailable"
    assert "not built" in result["semantic_unavailable"]
    assert result["count"] >= 1


@pytest.mark.asyncio
async def test_degrade_model_mismatch():
    """Model mismatch → state unavailable, advises rebuild."""
    await _seed_messages()
    _configure(model="bge-new")
    await vectorestore.record_index_build("bge-old", 4)

    result = await search_with_semantic(_kw_fn, "packaging", 10)

    assert result["semantic_state"] == "unavailable"
    assert "rebuild" in result["semantic_unavailable"]
    assert result["count"] >= 1


@pytest.mark.asyncio
async def test_degrade_endpoint_unreachable():
    """Embeddings endpoint down → keyword results, state unavailable."""
    await _seed_messages()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)

    def dead(request):
        raise httpx.ConnectError("down", request=request)

    embeddings._transport = _CapturingTransport(dead)
    try:
        result = await search_with_semantic(_kw_fn, "packaging", 10)
    finally:
        embeddings._transport = None

    assert result["semantic_state"] == "unavailable"
    assert "unreachable" in result["semantic_unavailable"]
    assert result["count"] >= 1


@pytest.mark.asyncio
async def test_degrade_dimension_drift():
    """Dimension drift → state unavailable, advises rebuild."""
    await _seed_messages()
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


# ============================================================
# Assembled scope fusion
# ============================================================

@pytest.mark.asyncio
async def test_global_search_fuses_across_workspaces():
    """Global search fuses keyword and semantic across all workspaces."""
    await _seed_messages()
    _configure()
    await vectorestore.record_index_build("bge-m3", 4)
    await vectorestore.store_chunk_embeddings([{
        "conversation_id": 3, "message_id": 5, "role": "user",
        "chunk_text": "The automated test suite is passing on the staging branch.",
        "chunk_index": 0, "embedding": [0.25, 0.25, 0.25, 0.25],
    }])

    transport = _respond()
    embeddings._transport = transport
    try:
        result = await search_with_semantic(_global_kw_fn, "test suite", 10)
    finally:
        embeddings._transport = None

    assert result["semantic_state"] == "ready"
    # Semantic hit from conversation 3 should be included
    ids = {m["message_id"] for m in result["matches"]}
    assert 5 in ids
    assert result["count"] >= 1


# ============================================================
# Endpoint shape
# ============================================================

@pytest.mark.asyncio
async def test_api_search_returns_fusion_shape():
    """GET /api/search returns the RRF fusion response shape."""
    await _seed_messages()

    from fastapi.testclient import TestClient
    import backend.main as main_mod

    client = TestClient(main_mod.app)

    # Not configured → keyword-only with state
    r = client.get("/api/search", params={"query": "packaging failed"})
    assert r.status_code == 200
    data = r.json()
    assert "semantic_state" in data
    assert "semantic_matches" in data
    assert "truncated" in data
    assert "matches" in data
    assert "count" in data

    # kw: prefix via API
    r2 = client.get("/api/search", params={"query": "kw:packaging"})
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["semantic_state"] == "off"
    assert data2["semantic_matches"] == 0