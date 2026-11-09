"""Vector store: SQLite-based embedding index (issue #677, Phase 2a).

Stores one row per message chunk in the vector_index table, plus the
index build metadata (embedding model name, dimensions, build time) in
app_meta. Dimensions are never hardcoded -- they come from the
embeddings endpoint response and are recorded per build, so a model
mismatch can be detected at query time and search degrades to
keyword-only instead of comparing incommensurable vectors.

Vector serialization is JSON (stdlib only, no numpy); similarity is
cosine, computed in Python over the rows loaded into memory.
"""

import asyncio
import json
import logging
import math
from datetime import datetime, timezone

from backend.db.database import open_db

log = logging.getLogger("yaah.vectorestore")

INDEX_METADATA_KEY = "search_index_metadata"

# Semantic readiness states (the degradation ladder):
#   not_configured - no endpoint/model in config; keyword-only is normal.
#   no_index       - configured, but the index was never built (or is empty).
#   mismatch       - the index was built with a different embedding model
#                    than the one now configured; querying it would compare
#                    incommensurable vectors, so it must not be used.
#   ready          - semantic search may run.
READINESS_STATES = ("not_configured", "no_index", "mismatch", "ready")


async def _get_index_metadata() -> dict:
    """Retrieve the metadata about the current vector index."""
    async with open_db() as db:
        cur = await db.execute(
            "SELECT value FROM app_meta WHERE key = ?", (INDEX_METADATA_KEY,)
        )
        row = await cur.fetchone()
        if row is None:
            return {}
        try:
            meta = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return {}
        return meta if isinstance(meta, dict) else {}


async def _set_index_metadata(metadata: dict):
    """Store metadata about the vector index."""
    async with open_db() as db:
        await db.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (INDEX_METADATA_KEY, json.dumps(metadata)),
        )
        await db.commit()


async def record_index_build(model_name: str, dimensions: int):
    """Record the embedding model and dimensions that built the index.

    Called after an index rebuild stored at least one vector, so query-time
    checks can detect a model mismatch. A build that stored nothing must
    NOT record metadata -- it would mask a no_index state as ready.
    """
    await _set_index_metadata({
        "model": model_name,
        "dimensions": dimensions,
        "last_build": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


async def get_index_metadata() -> dict:
    """Get the model name, dimensions, and last build time of the index."""
    return await _get_index_metadata()


def semantic_readiness(configured_model: str, metadata: dict | None = None) -> str:
    """Classify whether semantic search may run, and why not when it may not.

    Returns one of READINESS_STATES. Pure function of (configured_model,
    metadata) so tests exercise the ladder without touching the DB.
    """
    if not (configured_model or "").strip():
        return "not_configured"
    if not metadata:
        return "no_index"
    if not metadata.get("model"):
        return "no_index"
    if metadata.get("model") != configured_model:
        return "mismatch"
    return "ready"


async def detect_model_mismatch(configured_model: str) -> bool:
    """True when the configured model differs from the index model.

    A missing index is not a mismatch -- it is no_index (nothing to
    compare); readiness classification lives in semantic_readiness.
    """
    metadata = await _get_index_metadata()
    return semantic_readiness(configured_model, metadata) == "mismatch"


async def store_chunk_embeddings(rows: list[dict]) -> int:
    """Store embedding vectors for message chunks, one transaction.

    Each row: {conversation_id, message_id, role, chunk_text, chunk_index,
    embedding}. The INSERT OR REPLACE honors the UNIQUE(message_id,
    chunk_index) constraint so a rebuild over leftover rows repairs rather
    than errors. Returns the number of rows stored.
    """
    if not rows:
        return 0
    payloads = [
        (
            r["message_id"],
            r["conversation_id"],
            r["role"],
            r["chunk_text"],
            r["chunk_index"],
            json.dumps(r["embedding"]).encode("utf-8"),
        )
        for r in rows
    ]
    async with open_db() as db:
        try:
            await db.executemany(
                "INSERT OR REPLACE INTO vector_index (message_id, conversation_id, "
                "role, chunk_text, chunk_index, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                payloads,
            )
            await db.commit()
        except Exception as e:
            # A message deleted mid-rebuild violates the FK (foreign_keys
            # is ON). Store the survivors batch-by-batch instead of
            # aborting the whole build; deleted transcripts must not be
            # re-indexed anyway.
            if "FOREIGN KEY" not in str(e):
                raise
            stored = 0
            for p in payloads:
                try:
                    await db.execute(
                        "INSERT OR REPLACE INTO vector_index (message_id, "
                        "conversation_id, role, chunk_text, chunk_index, embedding) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        p,
                    )
                    await db.commit()
                    stored += 1
                except Exception:
                    if db.in_transaction:
                        await db.rollback()
            log.warning(
                "store_chunk_embeddings: skipped %d rows referencing deleted messages",
                len(payloads) - stored,
            )
            return stored
    return len(payloads)


async def clear_vector_index():
    """Drop all vectors from the index (for rebuild)."""
    async with open_db() as db:
        await db.execute("DELETE FROM vector_index")
        await db.commit()


async def clear_index_metadata():
    """Drop the build metadata along with the vectors.

    Readiness is a claim about the stored vectors; when a rebuild clears
    them before re-embedding, a surviving metadata row would keep
    readiness "ready" over an empty index. A build that stores nothing
    then reports no_index -- the honest state -- instead of fake-ready.
    """
    await _set_index_metadata({})


async def get_vector_count() -> int:
    """Count the number of embedded vectors in the index."""
    async with open_db() as db:
        cur = await db.execute("SELECT COUNT(*) AS count FROM vector_index")
        row = await cur.fetchone()
        return row["count"]


async def conversation_ids_in_workspaces(workspaces: list[str]) -> list[int]:
    """Conversation ids stored under any of the given workspace paths.

    Workspace paths are normalized here (the DB stores NULL for the
    default workspace) so callers pass raw config strings.
    """
    from backend.db.database import normalize_workspace

    normalized = []
    for ws in workspaces or []:
        norm = normalize_workspace(ws)
        if norm not in normalized:
            # None stays in the list: it IS the Default workspace (the
            # DB stores NULL for it), not an empty entry to skip.
            normalized.append(norm)
    if not normalized:
        return []
    non_null = [n for n in normalized if n is not None]
    async with open_db() as db:
        if None in normalized:
            # NULL workspace = Default workspace; '' normalizes to None.
            # Plain paths ride the same query when present.
            if non_null:
                placeholders = ",".join("?" * len(non_null))
                cur = await db.execute(
                    "SELECT id FROM conversations WHERE workspace IS NULL "
                    f"OR workspace IN ({placeholders})",
                    non_null,
                )
            else:
                cur = await db.execute(
                    "SELECT id FROM conversations WHERE workspace IS NULL"
                )
        else:
            placeholders = ",".join("?" * len(normalized))
            cur = await db.execute(
                f"SELECT id FROM conversations WHERE workspace IN ({placeholders})",
                normalized,
            )
        return [row["id"] for row in await cur.fetchall()]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def semantic_search(
    query_embedding: list[float],
    max_results: int = 10,
    conversation_ids: list[int] | None = None,
    min_similarity: float = 0.0,
) -> list[dict]:
    """Find stored chunks most similar to the query embedding.

    conversation_ids filters the scan (None = all conversations); it is
    compiled into the SQL IN clause rather than filtered in Python so a
    huge index does not get hauled into memory for a scoped search.
    Cosine similarity is computed in Python (stdlib only, no numpy).

    Rows whose stored vector length disagrees with the query are skipped
    with a warning -- that combination would produce a silent garbage
    score from zip truncation, and it means the index and the query were
    built by different models: degrade, never fake a result.
    """
    try:
        max_results = max(1, min(int(max_results or 10), 50))
    except (TypeError, ValueError):
        max_results = 10

    async with open_db() as db:
        if conversation_ids:
            placeholders = ",".join("?" * len(conversation_ids))
            cur = await db.execute(
                "SELECT id, message_id, conversation_id, role, chunk_text, embedding "
                f"FROM vector_index WHERE conversation_id IN ({placeholders})",
                conversation_ids,
            )
        else:
            cur = await db.execute(
                "SELECT id, message_id, conversation_id, role, chunk_text, embedding "
                "FROM vector_index"
            )
        rows = await cur.fetchall()

    def _score_rows():
        """Parse and score all rows. Runs in a worker thread: a big index
        must not block the event loop (agent streams stall otherwise)."""
        scored = []
        skipped = 0
        for row in rows:
            try:
                embedding = json.loads(bytes(row["embedding"]).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                skipped += 1
                continue
            # FK cascades remove vectors with their messages; rows written
            # before the FKs existed were dropped by the migration, and a
            # message deleted mid-build is refused at store time -- so
            # every row here has a live message behind it.
            if not isinstance(embedding, list) or len(embedding) != len(query_embedding):
                skipped += 1
                continue
            scored.append((
                _cosine_similarity(query_embedding, embedding),
                row,
            ))
        return scored, skipped

    scored, skipped = await asyncio.to_thread(_score_rows)
    if skipped:
        log.warning("semantic_search skipped %d unusable vectors", skipped)

    scored.sort(key=lambda pair: pair[0], reverse=True)
    results = []
    for similarity, row in scored[:max_results]:
        if similarity <= min_similarity:
            break
        results.append({
            "message_id": row["message_id"],
            "conversation_id": row["conversation_id"],
            "role": row["role"],
            "field": "content",
            "excerpt": row["chunk_text"],
            "similarity": round(similarity, 4),
        })
    return results
