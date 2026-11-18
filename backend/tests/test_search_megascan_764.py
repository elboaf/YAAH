"""Search megascan regression tests (#764).

The semantic scan once fetchalled the whole vector_index table per
query (1.15 GB of embedding JSON on the owner's machine, ~20 s of
json.loads per query, multi-GB RAM under the search box's per-keystroke
queries) -- the wedge read as an app crash. The contract now:

- The scan decodes rows batch-by-batch, never the whole table at once
  (probe: json.loads call count stays at one batch's worth).
- Results are identical top-K semantics regardless of batch boundaries.
- New writes pack float32 BLOBs (4 bytes/dim); legacy JSON-text rows
  still decode (dual-form read; the repair pass converts them).
- The one-shot boot repair converts JSON rows to BLOB once per database
  (marker-row semantics), and never raises.
"""
import json
import struct

import pytest

from backend.agent import vectorestore, embedding_blob_repair
from backend.db import database

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_vectors.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)
    monkeypatch.setattr(database, "_migrated", set())
    yield db_file


def _row(conversation_id, message_id, vec, chunk_index=0):
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "role": "user",
        "chunk_text": f"chunk {message_id}.{chunk_index}",
        "chunk_index": chunk_index,
        "embedding": list(vec),
    }


async def _seed_conversation(workspace="ws", first=True) -> int:
    conv = await database.create_conversation("megascan test", workspace)
    if first:
        await database.add_message(conv, "user", "seed transcript text")
    return conv


async def _insert_legacy_json_row(conv: int, message_id: int, vec):
    """Write a row in the pre-#764 on-disk form: JSON text in the BLOB
    column (what store_chunk_embeddings did before the packed format)."""
    await database.add_message(conv, "user", "legacy body")
    async with database.open_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO vector_index (message_id, conversation_id, "
            "role, chunk_text, chunk_index, embedding) VALUES (?, ?, ?, ?, ?, ?)",
            (
                message_id,
                conv,
                "user",
                f"legacy {message_id}",
                0,
                json.dumps(list(vec)).encode("utf-8"),
            ),
        )
        await db.commit()


async def test_scan_decodes_one_batch_not_whole_table(monkeypatch):
    """The megascan probe (#764): with more rows than one batch holds,
    the decode count stays bounded by the batch, not the row count, and
    the scan never hauls the table -- fetchall is forbidden and every
    fetch is batch-sized. The old code fetched and parsed every row on
    every query."""
    conv = await _seed_conversation()
    for _ in range(22):
        await database.add_message(conv, "user", "more message bodies")
    # Small batch so the test stays tiny; probe is count-based.
    monkeypatch.setattr(vectorestore, "SEMANTIC_SCAN_BATCH", 8)
    rows = [
        _row(conv, i + 1, (0.001 * (i % 10), 0.2, 0.3))
        for i in range(23)  # 23 rows = 3 batches at batch=8
    ]
    await vectorestore.store_chunk_embeddings(rows)

    import aiosqlite

    loads_calls = {"n": 0}
    real_loads = vectorestore.json.loads

    def counting_loads(*a, **kw):
        loads_calls["n"] += 1
        return real_loads(*a, **kw)

    monkeypatch.setattr(vectorestore.json, "loads", counting_loads)

    fetch_sizes = []
    real_fetchmany = aiosqlite.Cursor.fetchmany

    async def guarded_fetchmany(self, size=1):
        fetch_sizes.append(size)
        return await real_fetchmany(self, size)

    async def no_fetchall(self):
        raise AssertionError("whole-table fetchall() is the #764 megascan")

    monkeypatch.setattr(aiosqlite.Cursor, "fetchmany", guarded_fetchmany)
    monkeypatch.setattr(aiosqlite.Cursor, "fetchall", no_fetchall)

    hits = await vectorestore.semantic_search([0.001, 0.2, 0.3], max_results=5)
    assert len(hits) == 5
    # Blob rows never hit json.loads at all (float32 unpack path); the
    # fatal shape is "loads called once per row" (the old megascan).
    assert loads_calls["n"] <= vectorestore.SEMANTIC_SCAN_BATCH
    # Memory signature is the fetch: batches only, never one big pull.
    assert fetch_sizes, "scan must fetch via fetchmany"
    assert all(s <= vectorestore.SEMANTIC_SCAN_BATCH for s in fetch_sizes)


async def test_topk_correct_across_batch_boundaries(monkeypatch):
    """The best matches must survive batch streaming: winners planted in
    the last batch rank above everything in earlier batches, in order."""
    conv = await _seed_conversation()
    for _ in range(13):
        await database.add_message(conv, "user", "more message bodies")
    monkeypatch.setattr(vectorestore, "SEMANTIC_SCAN_BATCH", 4)
    rows = [_row(conv, i + 1, (0.5, -0.5, 0.5)) for i in range(10)]
    # Winners in the FINAL batch (id 13, 14): highest similarity. Cosine
    # is direction-only, so winners differ in direction, not magnitude.
    rows += [_row(conv, 13, (1, 1, 0)), _row(conv, 14, (1, 0, 0))]
    await vectorestore.store_chunk_embeddings(rows)
    hits = await vectorestore.semantic_search([1.0, 1.0, 1.0], max_results=3)
    assert [h["message_id"] for h in hits] == [13, 14, 1]  # id 1: first of the ties
    assert hits[0]["similarity"] > hits[1]["similarity"] > hits[2]["similarity"]


async def test_new_writes_are_packed_float32_blobs():
    """New rows store 4-byte-per-dim little-endian float32, not 22 KB
    JSON text -- 5x smaller and no JSON parse at scan time."""
    conv = await _seed_conversation()
    vec = [0.1, 0.2, 0.3]
    await vectorestore.store_chunk_embeddings([_row(conv, 1, vec)])
    async with database.open_db() as db:
        cur = await db.execute(
            "SELECT embedding, typeof(embedding) AS t FROM vector_index"
        )
        row = await cur.fetchone()
    assert row["t"] == "blob"
    assert row["embedding"] == struct.pack("<3f", *vec)
    # And the packed form still searches correctly.
    hits = await vectorestore.semantic_search(vec, max_results=5)
    assert len(hits) == 1
    assert hits[0]["similarity"] == pytest.approx(1.0, abs=1e-6)


async def test_legacy_json_rows_still_searchable():
    """Dual-form read: rows written before #764 (JSON text in the BLOB
    column) decode and rank like any other row -- the repair pass is
    allowed to lag behind live traffic."""
    conv = await _seed_conversation()
    await _insert_legacy_json_row(conv, 1, (0.1, 0.2, 0.3))
    await _insert_legacy_json_row(conv, 2, (0.9, 0.0, 0.0))
    hits = await vectorestore.semantic_search([1.0, 0.0, 0.0], max_results=5)
    assert [h["message_id"] for h in hits] == [2, 1]


async def test_corrupt_row_is_skipped_not_fatal():
    """Undecodable bytes (neither JSON nor float32) skip with a warning,
    same contract as the old malformed-JSON skip."""
    conv = await _seed_conversation()
    await _insert_legacy_json_row(conv, 1, (0.1, 0.2, 0.3))
    await database.add_message(conv, "user", "corrupt body")
    # A real vector row for message 2, then corrupted in place: the test
    # must exercise the decoder actually skipping a live row.
    await vectorestore.store_chunk_embeddings([_row(conv, 2, (0.9, 0.9, 0.9))])
    async with database.open_db() as db:
        cur = await db.execute("UPDATE vector_index SET embedding = ? WHERE message_id = 2", (b"\x01\x02\x03",))
        assert cur.rowcount == 1
        await db.commit()
    hits = await vectorestore.semantic_search([1.0, 0.0, 0.0], max_results=5)
    assert [h["message_id"] for h in hits] == [1]


async def test_repair_converts_json_rows_once():
    """The boot pass converts every legacy JSON row to packed float32,
    stamps its marker, and is a no-op on the next run."""
    conv = await _seed_conversation()
    await _insert_legacy_json_row(conv, 1, (0.1, 0.2, 0.3))
    await _insert_legacy_json_row(conv, 2, (0.4, 0.5, 0.6))
    # The TEXT storage form (str in the BLOB column) must convert too.
    await database.add_message(conv, "user", "text-form body")
    async with database.open_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO vector_index (message_id, conversation_id, "
            "role, chunk_text, chunk_index, embedding) VALUES (?, ?, ?, ?, ?, ?)",
            (3, conv, "user", "text form", 0, "[0.7, 0.7, 0.7]"),
        )
        await db.commit()
    await vectorestore.store_chunk_embeddings([_row(conv, 4, (0.7, 0.7, 0.7))])

    result = await embedding_blob_repair.repair_embedding_blobs()
    assert result.get("converted") == 3
    assert not result.get("error")

    async with database.open_db() as db:
        cur = await db.execute(
            "SELECT COUNT(*) AS c FROM vector_index "
            "WHERE typeof(embedding) = 'text' OR hex(embedding) LIKE '5B%'"
        )
        assert (await cur.fetchone())["c"] == 0
        cur = await db.execute(
            "SELECT value FROM app_meta WHERE key = ?",
            (embedding_blob_repair._MARKER_KEY,),
        )
        assert await cur.fetchone() is not None

    # Second run: marker stops it.
    again = await embedding_blob_repair.repair_embedding_blobs()
    assert again.get("skipped") is True

    # The converted rows still search correctly (float32 round trip).
    hits = await vectorestore.semantic_search([0.1, 0.2, 0.3], max_results=5)
    assert hits[0]["message_id"] == 1


async def test_repair_converts_in_batches(monkeypatch):
    """Conversion itself streams: a row count beyond one repair batch
    still converts fully (bounded-memory pass, not a table-wide haul)."""
    conv = await _seed_conversation()
    monkeypatch.setattr(embedding_blob_repair, "REPAIR_BATCH", 5)
    for i in range(12):
        await _insert_legacy_json_row(conv, i + 1, (0.1 * (i + 1), 0.2, 0.3))
    result = await embedding_blob_repair.repair_embedding_blobs()
    assert result.get("converted") == 12
    async with database.open_db() as db:
        cur = await db.execute(
            "SELECT COUNT(*) AS c FROM vector_index "
            "WHERE typeof(embedding) = 'text' OR hex(embedding) LIKE '5B%'"
        )
        assert (await cur.fetchone())["c"] == 0


class _FakeCursor:
    """Minimal async cursor for the repair loop: serves its page once,
    then empty -- mirroring LIMIT pagination."""

    def __init__(self, rows, db=None):
        self._rows = rows
        self._db = db
        self._served = False

    async def fetchone(self):
        return self._rows[0] if not self._served and self._rows else None

    async def fetchall(self):
        if self._served:
            return []
        self._served = True
        if self._db is not None:
            self._db.pages_left -= 1
        return self._rows

    def __await__(self):
        # aiosqlite cursors are awaitable: `cur = await db.execute(...)`
        # runs the query and yields the cursor.
        async def _run():
            return self
        return _run().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeDb:
    """In-memory db double for repair_embedding_blobs: records UPDATEs
    and the marker, answers the marker/count/page queries. Pages deplete
    like the real LIMIT pagination -- the repair loop must terminate."""

    def __init__(self, legacy_rows):
        self.legacy_rows = legacy_rows
        self.updates = []
        self.marker = None
        self.pages_left = 1

    def execute(self, sql, params=None):
        # Mirrors aiosqlite: execute() returns a cursor-like object that
        # doubles as the async context manager (awaited in __aenter__).
        sql = " ".join(sql.split())
        if sql.startswith("SELECT value FROM app_meta"):
            return _FakeCursor([("done",)] if self.marker else [])
        if sql.startswith("SELECT COUNT(*)"):
            return _FakeCursor([{"c": len(self.legacy_rows)}])
        if sql.startswith("SELECT id, embedding FROM vector_index"):
            rows = list(self.legacy_rows) if self.pages_left > 0 else []
            return _FakeCursor(rows, db=self)
        if sql.startswith("UPDATE vector_index"):
            self.updates.extend(params)
            return _FakeCursor([])
        if sql.startswith("INSERT OR REPLACE INTO app_meta"):
            self.marker = params
            return _FakeCursor([])
        raise AssertionError(f"unexpected sql: {sql}")

    async def executemany(self, sql, payloads):
        self.updates.extend(payloads)

    async def commit(self):
        pass


async def test_repair_skips_unrepresentable_row_without_aborting():
    """CR #765 round: a row whose JSON values exceed float32 range must
    be left unchanged and counted (unrepresentable), while the healthy
    rows in the same batch still convert and the marker is written --
    the old abort-on-pack shape stalled every later boot on the row."""
    healthy = {"id": 1, "embedding": b"[0.1, 0.2, 0.3]"}
    overflow = {"id": 2, "embedding": b"[1e40, 2.0, 3.0]"}
    garbage = {"id": 3, "embedding": b"[not numbers]"}
    db = _FakeDb([healthy, overflow, garbage])

    result = await embedding_blob_repair.repair_embedding_blobs.__wrapped__(db) \
        if hasattr(embedding_blob_repair.repair_embedding_blobs, "__wrapped__") \
        else await embedding_blob_repair._repair(db)

    assert not result.get("error")
    assert result["converted"] == 1  # only the healthy row
    assert result["unrepresentable"] == 1  # the 1e40 row, counted
    assert result["left_garbage"] == 1  # the non-numeric row
    assert db.marker is not None  # the pass still completed + marked
    # Only the healthy row is in the UPDATE payloads, packed float32.
    assert db.updates == [(struct.pack("<3f", 0.1, 0.2, 0.3), 1)]
