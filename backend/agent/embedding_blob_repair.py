"""One-time conversion of legacy JSON-text embeddings to packed float32
BLOBs (#764).

store_chunk_embeddings wrote vectors as JSON text until #764; on a real
index that is ~22 KB per row where packed float32 needs ~4 KB (5x the
file size, and json.loads for every row on every semantic query -- the
#764 megascan's other half). This pass rewrites the legacy rows in
place, batch-by-batch, never re-embedding: the bytes are a lossless
repack of the same numbers.

Marker-row semantics follow index_repair / legacy_residue: once per
database, marker written only after work actually happened, and boot
must never break over a repair failure.
"""

import json
import logging
import struct

log = logging.getLogger("yaah.embedding_blob_repair")

_MARKER_KEY = "embedding_blob_repair_764"

# Rows converted per transaction: the SELECT fetches this many legacy
# rows at a time (keyed on rowid) so even a huge index converts with
# one batch in memory.
REPAIR_BATCH = 500


async def _marker_set(db) -> bool:
    cur = await db.execute("SELECT value FROM app_meta WHERE key = ?", (_MARKER_KEY,))
    return await cur.fetchone() is not None


async def repair_embedding_blobs() -> dict:
    """The #764 conversion pass. Runs at most once per database (marker
    row). Never raises -- boot must not break over repair failures."""
    from backend.db.database import get_db

    try:
        db = await get_db()
    except Exception:  # noqa: BLE001 - nothing ran; next boot retries
        return {"error": "db unavailable"}

    # get_db hands out a fresh connection: close it on every path, or its
    # aiosqlite thread outlives the caller (a hung interpreter exit in
    # tests; a leaked handle in production).
    try:
        return await _repair(db)
    finally:
        await db.close()


async def _repair(db) -> dict:
    try:
        if await _marker_set(db):
            return {"skipped": True}

        # Legacy rows are JSON *bytes* in a BLOB column (the pre-#764
        # writer encoded before insert), so typeof='text' would miss
        # them. JSON payloads start with '[' (0x5B): match both storage
        # classes (bytes-BLOB and any str-TEXT form).
        legacy_pred = "(typeof(embedding) = 'text' OR hex(embedding) LIKE '5B%')"

        async with db.execute(
            f"SELECT COUNT(*) AS c FROM vector_index WHERE {legacy_pred}"
        ) as cur:
            row = await cur.fetchone()
        remaining = row["c"] if row else 0
        if not remaining:
            # Nothing to convert (fresh DB or already-packed index):
            # stay unmarked so a later converging boot can repair.
            return {"skipped": True, "nothing_to_convert": True}

        converted = 0
        last_rowid = 0
        while True:
            cur = await db.execute(
                "SELECT id, embedding FROM vector_index "
                f"WHERE id > ? AND {legacy_pred} ORDER BY id LIMIT ?",
                (last_rowid, REPAIR_BATCH),
            )
            batch = await cur.fetchall()
            if not batch:
                break
            last_rowid = batch[-1]["id"]
            payloads = []
            for row in batch:
                vec, ok = _decode_json_embedding(row["embedding"])
                if not ok:
                    continue  # garbage row: leave it, the scan skips it too
                payloads.append(
                    (struct.pack(f"<{len(vec)}f", *vec), row["id"])
                )
            if payloads:
                await db.executemany(
                    "UPDATE vector_index SET embedding = ? WHERE id = ?", payloads
                )
                await db.commit()
                converted += len(payloads)

        await db.execute(
            "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, 'done')",
            (_MARKER_KEY,),
        )
        await db.commit()
        log.info("embedding blob repair: converted %d rows to float32", converted)
        return {"converted": converted, "left_garbage": remaining - converted}

    except Exception as e:  # noqa: BLE001 - boot must not break
        log.warning("embedding blob repair failed: %s", e)
        return {"error": str(e)}


def _decode_json_embedding(raw) -> tuple[list[float], bool]:
    """Decode a legacy JSON-text embedding. Returns ([], False) for
    anything that is not a JSON list of numbers."""
    try:
        vec = json.loads(bytes(raw).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return [], False
    if isinstance(vec, list) and all(isinstance(x, (int, float)) for x in vec):
        return vec, True
    return [], False
