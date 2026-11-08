"""Index builder tests (issue #677, Phase 2a).

Chunking shape, batch embedding over real DB rows, metadata recording,
and abort/degrade behavior with a failing endpoint (capturing fake).
"""
import json

import httpx
import pytest

from backend.agent import config as config_mod
from backend.agent import embeddings, indexbuilder, vectorestore
from backend.db import database

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test_build.db")
    monkeypatch.setattr(database, "_migrated", set())
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")


class _CapturingTransport(httpx.MockTransport):
    def __init__(self, handler):
        super().__init__(handler)
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        self.bodies.append(json.loads(request.content.decode("utf-8")))
        return self.handler(request)


def _fake_endpoint(vectors_by_call: list[list[list[float]]], dim=4, model="m"):
    """Endpoint returning a scripted sequence of batches.

    vectors_by_call: list of batches, each batch is a list of vectors.
    If called more times than len(vectors_by_call), the last batch is
    reused (to handle variable chunking in tests). If vectors_by_call
    is empty, generate default vectors based on input count.
    """
    calls = {"n": 0}

    def handler(request):
        body = json.loads(request.content.decode("utf-8"))
        n_inputs = len(body["input"])
        if len(vectors_by_call) == 0:
            # No scripted batches: generate default vectors for all inputs
            result_vectors = [[0.25] * dim for _ in range(n_inputs)]
        else:
            i = min(calls["n"], len(vectors_by_call) - 1)
            vectors = vectors_by_call[i]
            # If the scripted vectors are shorter than the inputs, pad with zeros
            result_vectors = []
            for j in range(n_inputs):
                if j < len(vectors):
                    result_vectors.append(vectors[j])
                else:
                    result_vectors.append([0.0] * dim)
        calls["n"] += 1
        return httpx.Response(200, content=json.dumps({
            "data": [{"embedding": v} for v in result_vectors],
            "model": model,
        }).encode("utf-8"))

    return _CapturingTransport(handler)


def _configure(model="m", api_base="http://x"):
    config_mod.save_config({"embeddings": {
        "api_base": api_base, "api_key": "", "model": model,
    }})


async def _seed_messages(*texts: str, conversation_id: int = 1):
    await database.create_conversation("Build test", "ws")
    for t in texts:
        await database.add_message(conversation_id, "user", t)


# ---------------------------------------------------------------- chunking

def test_chunk_short_text_single_chunk():
    assert indexbuilder.chunk_text("short but over twenty chars") == [
        "short but over twenty chars"
    ]


def test_chunk_long_text_multiple_with_overlap():
    text = " ".join(f"word{i}" for i in range(600))  # ~4k chars
    chunks = indexbuilder.chunk_text(text, max_chars=500, overlap=100)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    # Overlap: chunk n+1 starts inside chunk n's tail.
    for a, b in zip(chunks, chunks[1:]):
        tail = a[-100:]
        assert any(b.startswith(tail[k:]) for k in range(0, 60, 10))


def test_chunk_empty_and_whitespace():
    assert indexbuilder.chunk_text("") == []
    assert indexbuilder.chunk_text("   ") == []


# ---------------------------------------------------------------- building

async def test_build_indexes_messages_and_records_metadata():
    _configure(model="bge-m3")
    await _seed_messages(
        "The deploy pipeline failed at the packaging stage yesterday.",
        "Rollback completed and the release was re-cut this morning.",
    )
    transport = _fake_endpoint([
        [[0.1] * 4, [0.2] * 4],
    ])
    # Batches: one batch of 2 chunks.
    import backend.agent.embeddings as emb_mod
    import backend.agent.indexbuilder as ib

    original = emb_mod.embed_texts

    async def routed(texts):
        return await original(texts)

    # Swap _transport at the embeddings module level.
    embeddings._transport = transport
    try:
        result = await ib.build_index()
    finally:
        embeddings._transport = None

    assert result["ok"] is True
    assert result["vectors_stored"] == 2
    assert result["dimensions"] == 4
    assert result["model"] == "bge-m3"
    # Metadata recorded: readiness is ready for this model.
    meta = await vectorestore.get_index_metadata()
    assert meta["model"] == "bge-m3"
    assert meta["dimensions"] == 4
    assert meta["last_build"]
    # Request captured: configured model named, both texts sent.
    assert transport.bodies[0]["model"] == "bge-m3"
    assert len(transport.bodies[0]["input"]) == 2


async def test_build_batches_requests():
    """More chunks than one batch: request count reflects the batch size."""
    _configure()
    await _seed_messages(*[f"message {i} with plenty of text here" for i in range(5)])
    transport = _fake_endpoint([])  # handler returns last batch shape
    # 5 chunks -> one batch of 5 (under 64), one request.
    embeddings._transport = transport
    try:
        result = await indexbuilder.build_index()
    finally:
        embeddings._transport = None
    assert result["ok"] is True
    assert len(transport.requests) == 1
    assert len(transport.bodies[0]["input"]) == 5


async def test_build_aborts_clean_when_endpoint_dead_from_start():
    """Endpoint down before any success: empty index, NO metadata
    (readiness stays no_index), error reported, nothing raises."""
    _configure()

    def dead(request):
        raise httpx.ConnectError("refused", request=request)

    transport = _CapturingTransport(dead)
    await _seed_messages("A transcript long enough to be indexed properly.")
    embeddings._transport = transport
    try:
        result = await indexbuilder.build_index()
    finally:
        embeddings._transport = None

    assert result["ok"] is False
    assert "error" in result
    assert result["vectors_stored"] == 0
    assert await vectorestore.get_index_metadata() == {}
    assert await vectorestore.get_vector_count() == 0


async def test_build_records_partial_on_mid_build_failure():
    """Endpoint dies after batch 1 stored: stored vectors remain, error
    reported, metadata reflects what WAS built (usable partial index)."""
    _configure()
    await _seed_messages(*[f"msg {i} text for embedding here" for i in range(3)])
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, content=json.dumps({
                "data": [{"embedding": [0.1] * 4}],
                "model": "m",
            }).encode("utf-8"))
        raise httpx.ConnectError("mid-build drop", request=request)

    transport = _CapturingTransport(flaky)
    # Force tiny batches so batch 1 = 1 chunk, batch 2 fails.
    import backend.agent.indexbuilder as ib
    original_batch = ib.EMBEDDING_BATCH_SIZE
    ib.EMBEDDING_BATCH_SIZE = 1
    embeddings._transport = transport
    try:
        result = await ib.build_index()
    finally:
        embeddings._transport = None
        ib.EMBEDDING_BATCH_SIZE = original_batch

    assert result["ok"] is False
    assert result["chunks_embedded"] == 1
    assert result["vectors_stored"] == 1
    meta = await vectorestore.get_index_metadata()
    assert meta["model"] == "m"


async def test_progress_callback_fires_through_phases():
    _configure()
    await _seed_messages("one transcript line long enough to embed fine")
    phases = []
    transport = _fake_endpoint([[[0.1] * 4]])
    embeddings._transport = transport
    try:
        await indexbuilder.build_index(
            progress_callback=lambda phase, pct, msg: phases.append(phase)
        )
    finally:
        embeddings._transport = None
    assert phases[0] == "starting"
    assert "embedding" in phases
    assert phases[-1] == "done"


async def test_broken_progress_callback_does_not_kill_build():
    _configure()
    await _seed_messages("one transcript line long enough to embed fine")

    def boom(phase, pct, msg):
        raise RuntimeError("UI broke")

    transport = _fake_endpoint([[[0.1] * 4]])
    embeddings._transport = transport
    try:
        result = await indexbuilder.build_index(progress_callback=boom)
    finally:
        embeddings._transport = None
    assert result["ok"] is True
    assert result["vectors_stored"] == 1


async def test_short_and_tool_messages_skipped():
    _configure()
    await _seed_messages("ok", "a perfectly adequate transcript message")
    await database.add_message(1, "tool", "tool result payload data here")
    transport = _fake_endpoint([[[0.1] * 4]])
    embeddings._transport = transport
    try:
        result = await indexbuilder.build_index()
    finally:
        embeddings._transport = None
    # Only the one adequate user message embedded.
    assert result["total_chunks"] == 1
