"""Index builder: reads chunks from the FTS index and embeds them.

Orchestrates the embeddings client and vector store to build the semantic
search index from existing FTS5 content. Called by the user-triggered
rebuild button in Settings.

The builder is designed to be called incrementally — it can rebuild the
entire index or just specific conversations.
"""

import logging
import time

from backend.agent.embeddings import embed_texts, EmbeddingsError
from backend.agent.vectorestore import (
    store_chunk_embeddings,
    clear_vector_index,
    record_index_build,
    get_vector_count,
)
from backend.db.database import open_db

log = logging.getLogger("yaah.indexbuilder")

# Chunk size: how many texts to send to the embeddings API in one request.
EMBEDDING_BATCH_SIZE = 64


def chunk_text(text: str, max_chars: int = 2000, overlap: int = 200) -> list[str]:
    """Split text into overlapping chunks for embedding."""
    if not text:
        return []
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end >= len(text):
            chunks.append(text[start:])
            break
        boundary = text.rfind(" ", start + max_chars // 2, end)
        if boundary == -1:
            boundary = end
        chunks.append(text[start:boundary])
        start = boundary - overlap
        if start < 0:
            start = 0
    return chunks


async def build_index(progress_callback=None) -> dict:
    """Build the semantic search index from all conversations.

    progress_callback is called with (phase, progress_pct, message).
    Returns a summary of the build.
    """

    def safe_report(phase, pct, msg):
        if progress_callback:
            try:
                progress_callback(phase, pct, msg)
            except Exception:
                # A broken callback (UI crashed) must not kill the build.
                log.warning("progress callback failed", exc_info=True)

    safe_report("starting", 0, "Starting index build")

    # Label identity must match what query time resolves (#734, #748):
    # the running bundled server wins, else the manual block. Reading the
    # manual block only made every bundled-server build record an empty
    # model name when the manual field was blank -- and semantic_readiness
    # treats an empty label as no_index, permanently degrading search.
    from backend.agent.embeddings import _resolved_connection
    conn = _resolved_connection()
    configured_model = conn["model"]
    if not configured_model:
        # Nothing resolves right now. embed_texts would reject the first
        # batch anyway; refuse here too so metadata never records an
        # unusable label.
        safe_report("error", 0, "no embeddings endpoint resolves -- build refused")
        return {
            "ok": False,
            "vectors_stored": 0,
            "chunks_processed": 0,
            "chunks_embedded": 0,
            "total_chunks": 0,
            "model": "",
            "dimensions": None,
            "last_build": time.time(),
            "error": "no embeddings endpoint resolves: start the bundled "
            "model server or configure an embeddings endpoint",
        }

    # Clear existing index -- and the build metadata with it. The metadata
    # must never outlive the vectors it describes: a failed rebuild would
    # otherwise leave "ready" pointing at an empty index.
    safe_report("clearing", 5, "Clearing existing index")
    await clear_vector_index()
    from backend.agent.vectorestore import clear_index_metadata
    await clear_index_metadata()

    # Read all messages
    safe_report("reading", 10, "Reading conversation data")

    messages = []
    async with open_db() as db:
        cur = await db.execute(
            "SELECT m.id, m.conversation_id, m.role, m.content "
            "FROM messages m WHERE m.role IN ('assistant', 'user')"
        )
        rows = await cur.fetchall()
        for row in rows:
            text = row["content"] or ""
            if len(text.strip()) < 20:
                continue
            messages.append({
                "message_id": row["id"],
                "conversation_id": row["conversation_id"],
                "role": row["role"],
                "text": text,
            })

    total_messages = len(messages)
    safe_report("chunking", 15, f"Chunking {total_messages} messages")

    # Chunk messages into embedding units
    chunks = []
    for msg in messages:
        msg_chunks = chunk_text(msg["text"])
        for i, chunk in enumerate(msg_chunks):
            chunks.append({
                "message_id": msg["message_id"],
                "conversation_id": msg["conversation_id"],
                "role": msg["role"],
                "chunk_index": i,
                "text": chunk,
            })

    total_chunks = len(chunks)
    safe_report("embedding", 20, f"Embedding {total_chunks} chunks")

    # Embed in batches
    embedded = 0
    dimensions = None
    last_error = None
    consecutive_failures = 0
    for batch_start in range(0, total_chunks, EMBEDDING_BATCH_SIZE):
        batch_end = min(batch_start + EMBEDDING_BATCH_SIZE, total_chunks)
        batch = chunks[batch_start:batch_end]

        texts = [c["text"] for c in batch]
        try:
            embeddings, dims, _ = await embed_texts(texts)
            if dimensions is None:
                dimensions = dims

            rows = []
            for i, (chunk, emb) in enumerate(zip(batch, embeddings)):
                rows.append({
                    "conversation_id": chunk["conversation_id"],
                    "message_id": chunk["message_id"],
                    "role": chunk["role"],
                    "chunk_text": chunk["text"],
                    "chunk_index": chunk["chunk_index"],
                    "embedding": emb,
                })
            stored = await store_chunk_embeddings(rows)
            embedded += stored

            if total_chunks > 0:
                pct = 20 + int(80 * embedded / total_chunks)
                safe_report("embedding", pct, f"Embedded {embedded}/{total_chunks} chunks")
            consecutive_failures = 0

        except EmbeddingsError as e:
            last_error = str(e)
            consecutive_failures += 1
            log.warning("Batch embedding failed: %s", e)
            # A dead endpoint fails every remaining batch; stop after a
            # few consecutive failures instead of grinding through the
            # whole corpus. A success resets the count, so a flaky
            # endpoint keeps going and a dead one bails fast.
            if consecutive_failures >= 3:
                safe_report(
                    "error", 20,
                    f"stopping: {consecutive_failures} consecutive embedding batches failed",
                )
                break

    # Record index metadata (model + dimensions). configured_model now
    # comes from the resolved connection (above), so the label matches
    # what query time checks against -- the #748 bug cannot recur.
    if dimensions and configured_model:
        await record_index_build(configured_model, dimensions)

    vector_count = await get_vector_count()

    result = {
        "ok": last_error is None,
        "vectors_stored": vector_count,
        "chunks_processed": embedded,
        "chunks_embedded": embedded,
        "total_chunks": total_chunks,
        "model": configured_model,
        "dimensions": dimensions,
        "last_build": time.time(),
    }
    if last_error:
        result["error"] = last_error

    safe_report("done", 100, f"Index build complete: {vector_count} vectors stored")

    return result