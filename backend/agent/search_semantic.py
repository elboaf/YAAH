"""Semantic search layer (issue #677, Phase 2a).

One degradation ladder shared by the agent tool (tools.py) and the HTTP
endpoint (main.py): embed the query, rank stored chunks by cosine
similarity, and on ANY failure return keyword results with
semantic_unavailable explaining why. Keyword search is the floor --
nothing here may turn an endpoint outage into a failed search.
"""

import logging

from backend.agent import embeddings, vectorestore
from backend.agent.config import load_config
from backend.agent.vectorestore import semantic_readiness

log = logging.getLogger("yaah.search_semantic")


async def search_with_semantic(
    keyword_fn,
    query: str,
    max_results: int,
    workspace_reach: str | None = None,
    workspace: str | None = None,
    conversation_ids: list[int] | None = None,
) -> dict:
    """Run keyword search with semantic boost/degradation.

    keyword_fn(query, max_results) -> {"matches": [...], ...} is the
    caller's existing keyword search (FTS). When semantic search is
    ready, matches are the semantic ranking (each hit carries a
    similarity score and the FTS keyword matches merge in below them --
    union, keyword order preserved for hits the index missed).

    Scope: an explicit conversation_ids list wins (the single-
    conversation tool path passes [conversation_id]); workspace reach
    narrows next; None (and no workspace reach) means all conversations
    -- the HTTP search is global by design.

    The response always carries the keyword contract shape plus:
      semantic_state - "ready" | "unavailable" | "off" | "not_configured"
      semantic_unavailable - human string, present iff state != ready
                             (and state != not_configured, where semantic
                             is simply not a feature).

    Semantic hits are enriched to the classified shape (kind +
    conversation identity) the T3 endpoint contract promises, and the
    merged list is re-classified per hit so every match the caller sees
    carries the same fields regardless of which half produced it.
    """
    import asyncio
    # keyword_fn may be sync or async; handle both.
    if asyncio.iscoroutinefunction(keyword_fn):
        result = await keyword_fn(query, max_results)
    else:
        result = keyword_fn(query, max_results)
        if asyncio.iscoroutine(result) or asyncio.isawaitable(result):
            result = await result
    if not isinstance(result, dict) or "matches" not in result:
        # Malformed keyword result: pass it through unchanged rather than
        # pretending semantic_state applies to a shape we do not own.
        return result

    emb_cfg = embeddings._embeddings_config()
    configured = bool(emb_cfg["api_base"]) and bool(emb_cfg["model"])
    if not configured:
        result["semantic_state"] = "not_configured"
        return result

    meta = await vectorestore.get_index_metadata()
    readiness = semantic_readiness(emb_cfg["model"], meta)
    if readiness == "not_configured":
        result["semantic_state"] = "not_configured"
        return result
    if readiness == "no_index":
        result["semantic_state"] = "unavailable"
        result["semantic_unavailable"] = "index not built yet"
        return result
    if readiness == "mismatch":
        result["semantic_state"] = "unavailable"
        result["semantic_unavailable"] = (
            f"index built with model '{meta.get('model')}' but '{emb_cfg['model']}' "
            "is configured now -- rebuild the index"
        )
        return result

    # Ready: embed the query. Unreachable endpoint degrades to keyword.
    try:
        query_vec, dims, _model = await embeddings.embed_text(query)
    except embeddings.EmbeddingsError as e:
        log.info("semantic search degraded (embed failed): %s", e)
        result["semantic_state"] = "unavailable"
        result["semantic_unavailable"] = f"embeddings endpoint unreachable: {e}"
        return result

    if meta.get("dimensions") and dims != meta["dimensions"]:
        # The endpoint changed dimensions since the build: comparing would
        # be garbage. Degrade, never fake a score.
        result["semantic_state"] = "unavailable"
        result["semantic_unavailable"] = (
            f"endpoint now returns {dims} dimensions but the index was built "
            f"with {meta['dimensions']} -- rebuild the index"
        )
        return result

    # Scope: an explicit conversation scope wins; workspace reach narrows
    # next; neither means all conversations (the HTTP search is global by
    # design). The keyword half and the semantic half of a response must
    # cover the SAME conversations.
    if conversation_ids:
        if not conversation_ids:
            result["semantic_state"] = "unavailable"
            result["semantic_unavailable"] = "no indexed conversations in scope"
            return result
    elif workspace_reach in ("workspace", "related"):
        from backend.db.database import get_related_workspaces
        scopes = [workspace] if workspace_reach == "workspace" else []
        if workspace_reach == "related":
            scopes = await get_related_workspaces(workspace or "")
        conversation_ids = await vectorestore.conversation_ids_in_workspaces(scopes)
        if not conversation_ids:
            result["semantic_state"] = "unavailable"
            result["semantic_unavailable"] = "no indexed conversations in scope"
            return result

    try:
        semantic_hits = await vectorestore.semantic_search(
            query_vec, max_results=max_results,
            conversation_ids=conversation_ids,
        )
    except Exception as e:  # a vector-store fault must not kill search
        log.warning("semantic scan failed; keyword-only", exc_info=True)
        result["semantic_state"] = "unavailable"
        result["semantic_unavailable"] = f"vector store error: {e}"
        return result

    if not semantic_hits:
        # Index ready but nothing scored above the floor: keep keyword
        # results untouched, report semantic found nothing.
        result["semantic_state"] = "ready"
        result["semantic_matches"] = 0
        return result

    # Merge: semantic hits first (they carry similarity), then keyword
    # matches the index missed, deduped by message_id. Every hit is
    # classified (kind: chat/tool/sub-agent) so a merged list has ONE
    # contract shape regardless of which half produced each match.
    from backend.db.database import _classify_result_kind

    keyword_matches = result.get("matches") or []
    seen = set()
    merged = []
    for hit in semantic_hits:
        hit.setdefault("kind", _classify_result_kind(hit))
        seen.add(hit["message_id"])
        merged.append(hit)
    for m in keyword_matches:
        if m.get("message_id") not in seen:
            m.setdefault("kind", _classify_result_kind(m))
            merged.append(m)
            seen.add(m.get("message_id"))

    result["matches"] = merged[:max_results]
    result["count"] = len(result["matches"])
    result["semantic_state"] = "ready"
    result["semantic_matches"] = len(semantic_hits)
    return result
