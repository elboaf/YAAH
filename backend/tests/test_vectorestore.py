"""Vector store tests (issue #677, Phase 2a).

Store/query round trips, the readiness ladder (including model
mismatch), and dimension-mismatch rejection in semantic_search.
"""
import pytest

from backend.agent import vectorestore
from backend.db import database

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_vectors.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)
    monkeypatch.setattr(database, "_migrated", set())
    yield db_file


def _row(conversation_id=1, message_id=1, vec=(0.1, 0.2, 0.3), chunk_index=0):
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "role": "user",
        "chunk_text": f"chunk {message_id}.{chunk_index}",
        "chunk_index": chunk_index,
        "embedding": list(vec),
    }


async def test_store_and_query_round_trip():
    await vectorestore.store_chunk_embeddings([_row(message_id=1)])
    assert await vectorestore.get_vector_count() == 1
    hits = await vectorestore.semantic_search([0.1, 0.2, 0.3], max_results=5)
    assert len(hits) == 1
    assert hits[0]["message_id"] == 1
    assert hits[0]["similarity"] == pytest.approx(1.0, abs=1e-6)


async def test_commit_survives_connection_close():
    """The write must commit before the connection closes: a fresh
    connection sees the row (the aiosqlite rollback-on-close trap)."""
    await vectorestore.store_chunk_embeddings([_row(message_id=7)])
    async with database.open_db() as db:
        cur = await db.execute("SELECT COUNT(*) AS c FROM vector_index")
        assert (await cur.fetchone())["c"] == 1


async def test_similarity_ranking_order():
    near = (0.9, 0.0, 0.0)
    far = (0.1, 0.9, 0.0)  # positive similarity, but clearly weaker
    await vectorestore.store_chunk_embeddings([
        _row(message_id=1, vec=near),
        _row(message_id=2, vec=far),
    ])
    hits = await vectorestore.semantic_search([1.0, 0.0, 0.0], max_results=5)
    assert [h["message_id"] for h in hits] == [1, 2]
    assert hits[0]["similarity"] > hits[1]["similarity"] > 0


async def test_dimension_mismatch_rows_are_skipped():
    """A stored vector whose length differs from the query is skipped,
    never scored by zip-truncation garbage."""
    await vectorestore.store_chunk_embeddings([
        _row(message_id=1, vec=(0.1, 0.2, 0.3)),
        _row(message_id=2, vec=(0.5, 0.5)),  # wrong length
    ])
    hits = await vectorestore.semantic_search([0.1, 0.2, 0.3], max_results=5)
    assert [h["message_id"] for h in hits] == [1]


async def test_conversation_scope_filters():
    await vectorestore.store_chunk_embeddings([
        _row(conversation_id=1, message_id=1),
        _row(conversation_id=2, message_id=2),
    ])
    hits = await vectorestore.semantic_search(
        [0.1, 0.2, 0.3], max_results=10, conversation_ids=[2]
    )
    assert [h["message_id"] for h in hits] == [2]


def test_readiness_ladder():
    """not_configured < no_index < mismatch < ready, as pure function."""
    assert vectorestore.semantic_readiness("", None) == "not_configured"
    assert vectorestore.semantic_readiness("  ", {}) == "not_configured"
    assert vectorestore.semantic_readiness("m1", None) == "no_index"
    assert vectorestore.semantic_readiness("m1", {}) == "no_index"
    assert vectorestore.semantic_readiness("m1", {"model": ""}) == "no_index"
    assert vectorestore.semantic_readiness("m1", {"model": "m0"}) == "mismatch"
    assert vectorestore.semantic_readiness("m1", {"model": "m1"}) == "ready"


async def test_record_build_and_mismatch_detection():
    await vectorestore.record_index_build("m-old", 384)
    assert await vectorestore.detect_model_mismatch("m-new") is True
    assert await vectorestore.detect_model_mismatch("m-old") is False


async def test_no_index_is_not_a_mismatch():
    assert await vectorestore.detect_model_mismatch("never-built") is False


async def test_clear_vector_index():
    await vectorestore.store_chunk_embeddings([_row(message_id=1)])
    await vectorestore.clear_vector_index()
    assert await vectorestore.get_vector_count() == 0


async def test_metadata_cleared_state_reads_no_index():
    """Fresh DB: no metadata row -> readiness no_index even with a model
    configured (index never built)."""
    meta = await vectorestore.get_index_metadata()
    assert meta == {}
    assert vectorestore.semantic_readiness("m1", meta) == "no_index"


async def test_rebuild_replaces_rows_not_duplicates():
    """INSERT OR REPLACE on UNIQUE(message_id, chunk_index): re-storing
    the same chunk updates it instead of erroring."""
    await vectorestore.store_chunk_embeddings([_row(message_id=1)])
    await vectorestore.store_chunk_embeddings(
        [_row(message_id=1, vec=(0.9, 0.9, 0.9))]
    )
    assert await vectorestore.get_vector_count() == 1
    hits = await vectorestore.semantic_search([0.9, 0.9, 0.9], max_results=5)
    assert hits[0]["similarity"] == pytest.approx(1.0, abs=1e-6)


async def test_zero_vector_scores_zero_not_crash():
    await vectorestore.store_chunk_embeddings([_row(message_id=1, vec=(0.0, 0.0, 0.0))])
    hits = await vectorestore.semantic_search([0.1, 0.2, 0.3], max_results=5)
    assert hits == []  # similarity 0.0 falls below the > 0 floor
