"""Index-label repair tests (#748).

The one-time boot pass re-labels a healthy vector index whose metadata
records an empty model name (builds made through the bundled server
while the manual block was blank). Guards, marker semantics, fallback.
"""
import pytest

from backend.agent import config as config_mod
from backend.agent import embeddings, index_repair, vectorestore
from backend.db import database

pytestmark = pytest.mark.asyncio

_MARKER_KEY = "index_label_repair_748"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test_repair.db")
    monkeypatch.setattr(database, "_migrated", set())
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")


async def _seed_broken_index():
    """The #748 signature: vectors exist, label empty."""
    await database.create_conversation("repair test", "ws")
    await database.add_message(
        1, "user", "a persisted message long enough to matter to search"
    )
    await vectorestore.store_chunk_embeddings([{
        "message_id": 1,
        "conversation_id": 1,
        "role": "user",
        "chunk_text": "some chunk text",
        "chunk_index": 0,
        "embedding": [0.1, 0.2],
    }])
    await vectorestore.record_index_build("", 2)  # empty model label


async def _marker_exists() -> bool:
    from backend.db.database import open_db

    async with open_db() as db:
        cur = await db.execute("SELECT value FROM app_meta WHERE key = ?", (_MARKER_KEY,))
        return await cur.fetchone() is not None


async def test_repairs_empty_label_from_resolved_connection(monkeypatch):
    await _seed_broken_index()
    monkeypatch.setattr(
        embeddings, "_resolved_connection",
        lambda: {"api_base": "", "api_key": "", "model": "m2"},
    )
    result = await index_repair.repair_index_label()
    assert result == {"repaired": True, "model": "m2", "vectors": 1}

    meta = await vectorestore.get_index_metadata()
    assert meta["model"] == "m2"
    assert meta["dimensions"] == 2  # untouched
    assert vectorestore.semantic_readiness("m2", meta) == "ready"
    assert await _marker_exists() is True
    # One-time: a second run is a no-op.
    assert await index_repair.repair_index_label() == {"skipped": True}


async def test_falls_back_to_manifest_default_when_nothing_resolves(monkeypatch):
    await _seed_broken_index()
    monkeypatch.setattr(
        embeddings, "_resolved_connection",
        lambda: {"api_base": "", "api_key": "", "model": ""},
    )
    result = await index_repair.repair_index_label()
    assert result["repaired"] is True
    assert result["model"] == "qwen3-embedding-0.6b"


async def test_skips_when_label_present():
    await _seed_broken_index()
    await vectorestore.record_index_build("already-labeled", 2)
    result = await index_repair.repair_index_label()
    assert result == {"skipped": True, "reason": "label present"}
    assert await _marker_exists() is False  # stays unmarked


async def test_skips_and_stays_unmarked_when_index_empty():
    await vectorestore.record_index_build("", 2)  # metadata, no vectors
    result = await index_repair.repair_index_label()
    assert result == {"skipped": True, "reason": "empty index"}
    meta = await vectorestore.get_index_metadata()
    assert meta["model"] == ""  # label untouched
    assert await _marker_exists() is False
