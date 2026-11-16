"""One-time index-label repair (#748).

The Phase 2a index builder labeled builds from the manual embeddings
config block only, while query time resolves the running bundled server
first (the #734 resolution). A rebuild made through the bundled server
with the manual block left blank therefore recorded an empty model name
-- and semantic_readiness() maps an empty model to no_index, so every
search degraded to keyword-only with "index not built yet" despite a
fully built, healthy index (47k+ vectors on the owner's machine).

The vectors themselves were never wrong -- the build embedded through
the very server whose model id went missing -- so this pass repairs the
LABEL in place, once per database, at boot. Never re-embeds.

Guards: metadata exists, its model field is empty, dimensions were
recorded, and at least one vector row exists. The identity written is
what query time resolves right now (the running bundled server wins,
else the manual block), falling back to the manifest's default
embedding model when neither resolves -- a blank manual block is the
bug's signature, so the default id is the normal answer here. The
dimensions guard is advisory: a same-dimension different-model build
under an empty label is indistinguishable, same as ADR-0021's stored
label.

Marker-row semantics follow legacy_residue's one-time pattern, but the
marker is only written when a repair actually happens: skip paths
(missing metadata, label already present, no vectors) stay unmarked so
a later boot can repair once the state converges.
"""

import json
import logging

log = logging.getLogger(__name__)

_MARKER_KEY = "index_label_repair_748"


async def _marker_set(db) -> bool:
    cur = await db.execute("SELECT value FROM app_meta WHERE key = ?", (_MARKER_KEY,))
    return await cur.fetchone() is not None


async def repair_index_label() -> dict:
    """The #748 pass. Runs at most once per database (marker row).
    Never raises -- boot must not break over repair failures."""
    from backend.db.database import get_db

    try:
        db = await get_db()
    except Exception:  # noqa: BLE001 - nothing ran; next boot retries
        return {"error": "db unavailable"}

    try:
        if await _marker_set(db):
            return {"skipped": True}

        from backend.agent.vectorestore import INDEX_METADATA_KEY

        cur = await db.execute(
            "SELECT value FROM app_meta WHERE key = ?", (INDEX_METADATA_KEY,)
        )
        row = await cur.fetchone()
        if row is None:
            return {"skipped": True, "reason": "no index metadata"}
        try:
            meta = json.loads(row["value"])
        except (ValueError, TypeError):
            return {"skipped": True, "reason": "unreadable metadata"}
        if not isinstance(meta, dict) or meta.get("model"):
            return {"skipped": True, "reason": "label present"}
        dimensions = meta.get("dimensions")
        if not dimensions:
            return {"skipped": True, "reason": "no dimensions recorded"}
        cur = await db.execute("SELECT COUNT(*) FROM vector_index")
        count = (await cur.fetchone())[0]
        if not count:
            return {"skipped": True, "reason": "empty index"}

        from backend.agent import embeddings, modelserver

        conn = embeddings._resolved_connection()
        model_id = (conn.get("model") or "").strip()
        if not model_id:
            model_id = modelserver.default_model("embedding")["id"]

        meta["model"] = model_id
        await db.execute(
            "UPDATE app_meta SET value = ? WHERE key = ?",
            (json.dumps(meta), INDEX_METADATA_KEY),
        )
        # Marker after the guarded checks but before commit: a crash here
        # rolls both rows back together, so the pass re-runs next boot.
        await db.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?)",
            (_MARKER_KEY, "done"),
        )
        await db.commit()
        log.info(
            "index-label repair (#748): relabeled %d-vector index as %r",
            count, model_id,
        )
        return {"repaired": True, "model": model_id, "vectors": count}
    except Exception as e:  # noqa: BLE001 - boot must not break
        return {"error": str(e)}
    finally:
        await db.close()
