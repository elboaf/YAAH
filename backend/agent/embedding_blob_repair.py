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

import asyncio
import json
import logging
import math
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
    # tests; a leaked handle in production). Close failures are logged
    # and never override the repair result -- an exception here would
    # otherwise abort startup even after a successful, committed repair.
    try:
        return await _repair(db)
    finally:
        try:
            await db.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - cleanup must not win
            log.warning("embedding blob repair: close failed: %s", e)


async def _repair(db) -> dict:
    try:
        if await _marker_set(db):
            return {"skipped": True}

        # Legacy rows are JSON payloads in the embedding column (the
        # pre-#764 writer encoded before insert): JSON text (str) or
        # JSON bytes starting with '[' (0x5B). substr(...) = X'5B' tests
        # one byte; hex() would render every full BLOB to hex on each
        # boot even when nothing needs converting.
        legacy_pred = (
            "typeof(embedding) = 'text' "
            "OR substr(embedding, 1, 1) = X'5B'"
        )

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
        unrepresentable = 0
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
                # Pre-check float32 representability instead of catching
                # struct.pack's OverflowError: explicit, and it keeps a
                # value like 1e40 from aborting the pass before the
                # marker (which would stall every later boot on the same
                # row). NaN/inf rows stay unconverted; the scan skips
                # them either way (its isfinite guard).
                if not all(math.isfinite(v) and abs(v) <= 3.4028235e38 for v in vec):
                    unrepresentable += 1
                    continue
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
        return {
            "converted": converted,
            "unrepresentable": unrepresentable,
            "left_garbage": remaining - converted - unrepresentable,
        }

    except Exception as e:  # noqa: BLE001 - boot must not break
        log.warning("embedding blob repair failed: %s", e)
        return {"error": str(e)}


def _decode_json_embedding(raw) -> tuple[list[float], bool]:
    """Decode a legacy JSON embedding: SQLite TEXT (str) directly, or
    the historical JSON-bytes BLOB form via UTF-8. Returns ([], False)
    for anything that is not a JSON list of numbers."""
    if isinstance(raw, str):
        text = raw
    else:
        try:
            text = bytes(raw).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return [], False
    try:
        vec = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return [], False
    if isinstance(vec, list) and all(isinstance(x, (int, float)) for x in vec):
        return vec, True
    return [], False
