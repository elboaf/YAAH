"""Hybrid search with reciprocal rank fusion (issue #725, Phase 3).

Two retrievers run in parallel:
  - Keyword leg: SQLite FTS5 trigram (bm25-ordered, order-only rank)
  - Semantic leg: cosine similarity against stored chunk embeddings

Fusion uses Reciprocal Rank Fusion (RRF) with k=60, equal leg weights.
Each leg contributes 1/(k + rank) per message, summed across legs.
Deep slice K=100 per leg before fusion; fused list truncated to
caller's max_results. Per-message aggregation: best chunk per leg.

The keyword leg is the model-free floor: any semantic failure
(unconfigured, no index, mismatch, endpoint down, dimension drift,
store fault) degrades to keyword-only with semantic_state explaining
why. An explicit kw: prefix forces keyword-only with semantic_state "off".
"""

import asyncio
import inspect
import logging

from backend.agent import embeddings, reranker, vectorestore, modelserver
from backend.agent.vectorestore import semantic_readiness

log = logging.getLogger("yaah.search_semantic")

# RRF smoothing constant (ADR-0023 §1)
RRF_K = 60
# Deep slice per leg before fusion (ADR-0023 §2)
FUSION_DEPTH = 100


def rrf_score(rank: int) -> float:
    """Contribution of a single leg for a message at the given rank (1-based)."""
    return 1.0 / (RRF_K + rank)


def _aggregate_leg(hits: list[dict], field: str = "message_id") -> dict[int, dict]:
    """Per-message aggregation: best chunk per leg wins (ADR-0023 §3).

    Returns {message_id: hit} where hit is the highest-ranked chunk for
    that message in this leg.
    """
    best = {}
    for i, hit in enumerate(hits):
        mid = hit.get(field)
        if mid is None:
            continue
        if mid not in best:
            best[mid] = hit
    return best


async def search_with_semantic(
    keyword_fn,
    query: str,
    max_results: int,
    workspace_reach: str | None = None,
    workspace: str | None = None,
    conversation_ids: list[int] | None = None,
    rerank: bool = True,
) -> dict:
    """Run hybrid search with RRF fusion of keyword and semantic legs.

    keyword_fn(query, max_results) -> {"matches": [...], ...} is the
    caller's existing keyword search (FTS). It may be a coroutine
    function, a sync function, or a function that returns an awaitable
    (e.g. a coroutine object created from a local async def). All three
    patterns are supported.

    kw: prefix opt-out (ADR-0023 §4): if the query starts with "kw:",
    the prefix is stripped, only the keyword leg runs, and the response
    carries semantic_state "off". This state was promised by the contract
    but no code path produced it before Phase 3.

    rerank: if True, rerank the top fused results with a cross-encoder
    reranker if one is available (ADR-0023 §9). Fail-open: on reranker
    unavailability or error, the RRF-fused order is preserved.

    The response always carries the keyword contract shape plus:
      semantic_state - "ready" | "unavailable" | "off" | "not_configured"
      semantic_unavailable - human string, present iff state == "unavailable"
      semantic_matches - count of fused hits with a semantic contribution
      truncated - true if the fused candidate list exceeded max_results
      reranker_state - "applied" | "unavailable" | "not_configured" | "off"
    Each hit gains an optional sources: ["keyword", "semantic"] array
    indicating which legs contributed.
    """
    query = query.strip()

    # Validate/normalize max_results (matching old behavior and vectorestore bounds)
    try:
        max_results = max(1, min(int(max_results or 10), 50))
    except (TypeError, ValueError):
        max_results = 10

    async def _invoke_keyword_fn(q, n):
        """Handle all keyword_fn calling conventions."""
        if inspect.iscoroutinefunction(keyword_fn):
            return await keyword_fn(q, n)
        result = keyword_fn(q, n)
        if asyncio.iscoroutine(result) or inspect.isawaitable(result):
            return await result
        return result

    # kw: prefix opt-out (ADR-0023 §4)
    if query.startswith("kw:"):
        query = query[3:].strip()
        result = await _invoke_keyword_fn(query, max_results)
        if isinstance(result, dict) and "matches" in result:
            result["semantic_state"] = "off"
            result["semantic_matches"] = 0
        return result

    # Run both legs in parallel (ADR-0023 §5)
    kw_task = asyncio.create_task(_invoke_keyword_fn(query, FUSION_DEPTH))
    semantic_task = _run_semantic_leg(
        query, workspace_reach, workspace, conversation_ids
    )

    # Handle each leg separately so a semantic failure preserves keyword results
    kw_result, sem_result = await asyncio.gather(
        kw_task, semantic_task, return_exceptions=True
    )

    if isinstance(kw_result, BaseException):
        # Includes CancelledError (BaseException, not Exception): a
        # cancelled keyword leg must not be treated as a result value.
        # No fallback: keyword_fn itself is broken.
        log.warning("keyword leg failed", exc_info=kw_result)
        raise kw_result

    if not isinstance(kw_result, dict) or "matches" not in kw_result:
        return kw_result

    kw_matches = kw_result["matches"]

    def _degraded_truncated() -> bool:
        """Fused-truth truncation for keyword-only/degraded responses.

        The keyword leg ran at FUSION_DEPTH, so its own truncated flag is
        relative to that depth; the caller's cut is max_results.
        """
        return len(kw_matches) > max_results or bool(kw_result.get("truncated", False))

    if isinstance(sem_result, BaseException):
        # Semantic leg crashed (incl. cancellation); preserve keyword
        # results with unavailable state
        log.warning("semantic leg failed", exc_info=sem_result)
        kw_result["semantic_state"] = "unavailable"
        kw_result["semantic_unavailable"] = f"semantic search error: {sem_result}"
        kw_result["semantic_matches"] = 0
        kw_result["truncated"] = _degraded_truncated()
        kw_result["matches"] = kw_matches[:max_results]
        kw_result["count"] = len(kw_result["matches"])
        return kw_result

    if sem_result["state"] != "ready":
        kw_result["semantic_state"] = sem_result["state"]
        if sem_result.get("unavailable"):
            kw_result["semantic_unavailable"] = sem_result["unavailable"]
        kw_result["semantic_matches"] = 0
        kw_result["truncated"] = _degraded_truncated()
        kw_result["matches"] = kw_matches[:max_results]
        kw_result["count"] = len(kw_result["matches"])
        return kw_result

    # Fuse the two legs
    fused = _fuse(kw_matches, sem_result["hits"], kw_result, max_results)

    # Rerank with cross-encoder if available (ADR-0023 §9)
    if rerank and fused["semantic_state"] == "ready":
        try:
            fused = await _rerank_fused(query, fused, max_results)
        except Exception:
            log.warning("rerank failed; keeping RRF order", exc_info=True)
            fused["reranker_state"] = "unavailable"

    return fused


async def _run_semantic_leg(
    query: str,
    workspace_reach: str | None,
    workspace: str | None,
    conversation_ids: list[int] | None,
) -> dict:
    """Run the semantic leg and return {"state", "hits"?, "unavailable"?}."""
    emb_cfg = embeddings._embeddings_config()
    configured = bool(emb_cfg["api_base"]) and bool(emb_cfg["model"])
    if not configured:
        return {"state": "not_configured"}

    meta = await vectorestore.get_index_metadata()
    readiness = semantic_readiness(emb_cfg["model"], meta)
    if readiness == "not_configured":
        return {"state": "not_configured"}
    if readiness == "no_index":
        return {"state": "unavailable", "unavailable": "index not built yet"}
    if readiness == "mismatch":
        return {
            "state": "unavailable",
            "unavailable": (
                f"index built with model '{meta.get('model')}' but "
                f"'{emb_cfg['model']}' is configured now -- rebuild the index"
            ),
        }

    # Ready: embed the query
    try:
        query_vec, dims, _model = await embeddings.embed_text(query)
    except embeddings.EmbeddingsError as e:
        return {
            "state": "unavailable",
            "unavailable": f"embeddings endpoint unreachable: {e}",
        }

    if meta.get("dimensions") and dims != meta["dimensions"]:
        return {
            "state": "unavailable",
            "unavailable": (
                f"endpoint now returns {dims} dimensions but the index "
                f"was built with {meta['dimensions']} -- rebuild the index"
            ),
        }

    # Resolve scope (same as keyword leg)
    if conversation_ids:
        if not conversation_ids:
            return {
                "state": "unavailable",
                "unavailable": "no indexed conversations in scope",
            }
    elif workspace_reach in ("workspace", "related"):
        from backend.db.database import get_related_workspaces

        scopes = []
        if workspace_reach == "workspace":
            scopes = [
                workspace or "",
                *(await get_related_workspaces(workspace or "")),
            ]
        else:
            scopes = await get_related_workspaces(workspace or "")
        conversation_ids = await vectorestore.conversation_ids_in_workspaces(scopes)
        if not conversation_ids:
            return {
                "state": "unavailable",
                "unavailable": "no indexed conversations in scope",
            }

    try:
        hits = await vectorestore.semantic_search(
            query_vec, max_results=FUSION_DEPTH, conversation_ids=conversation_ids,
        )
    except Exception as e:
        log.warning("semantic scan failed; keyword-only", exc_info=True)
        return {
            "state": "unavailable",
            "unavailable": f"vector store error: {e}",
        }

    return {"state": "ready", "hits": hits}


def _fuse(
    kw_matches: list[dict],
    sem_hits: list[dict],
    result: dict,
    max_results: int,
) -> dict:
    """RRF fusion of keyword and semantic results (ADR-0023 §1-3, §6).

    1. Aggregate per-leg to per-message (best chunk wins)
    2. Sum RRF contributions across legs
    3. Sort: fused score desc → keyword rank asc → message_id asc
    4. Truncate to max_results
    5. Emit sources array and redefine semantic_matches
    """
    from backend.db.database import _classify_result_kind

    # Aggregate: best chunk per message per leg
    kw_by_msg = _aggregate_leg(kw_matches, "message_id")
    sem_by_msg = _aggregate_leg(sem_hits, "message_id")

    # Compute fused scores. Ranks are per-message positions in each
    # deduplicated leg dict (insertion order = the leg's ranking).
    fused = {}
    for rank, (mid, hit) in enumerate(kw_by_msg.items(), start=1):
        score = rrf_score(rank)
        fused[mid] = {
            "score": score,
            "kw_rank": rank,
            "kw_hit": hit,
            "sem_hit": None,
            "sources": ["keyword"],
        }

    for rank, (mid, hit) in enumerate(sem_by_msg.items(), start=1):
        if mid in fused:
            fused[mid]["score"] += rrf_score(rank)
            fused[mid]["sem_hit"] = hit
            fused[mid]["sources"].append("semantic")
        else:
            fused[mid] = {
                "score": rrf_score(rank),
                "kw_rank": float("inf"),
                "kw_hit": None,
                "sem_hit": hit,
                "sources": ["semantic"],
            }

    # Sort: fused score desc → keyword rank asc → message_id asc
    sorted_msgs = sorted(
        fused.items(),
        key=lambda pair: (-pair[1]["score"], pair[1]["kw_rank"], pair[0]),
    )

    # Build final result
    final_matches = []
    semantic_match_count = 0
    for mid, info in sorted_msgs:
        if len(final_matches) >= max_results:
            break

        # On collision, keyword leg's literal excerpt wins (ADR-0023 §3)
        if info["kw_hit"] is not None:
            match = dict(info["kw_hit"])
        else:
            match = dict(info["sem_hit"])

        # Attach sources and similarity; preserve semantic hit's identity
        # fields (conversation_id, similarity) even when the keyword hit
        # provides the excerpt
        match["sources"] = info["sources"]
        if info["sem_hit"] is not None:
            for field in ("conversation_id", "similarity"):
                if field not in match and field in info["sem_hit"]:
                    match[field] = info["sem_hit"][field]
        if info["sem_hit"] is not None and "similarity" in info["sem_hit"]:
            match["similarity"] = info["sem_hit"]["similarity"]

        # Classify kind (same for both legs' hits)
        match.setdefault("kind", _classify_result_kind(match))

        final_matches.append(match)

        if "semantic" in info["sources"]:
            semantic_match_count += 1

    # Truncated = fused candidates beyond the cut (ADR-0023 §7)
    truncated = len(fused) > max_results

    result["matches"] = final_matches
    result["count"] = len(final_matches)
    result["truncated"] = truncated
    result["semantic_state"] = "ready"
    result["semantic_matches"] = semantic_match_count

    return result


async def _rerank_fused(
    query: str,
    result: dict,
    max_results: int,
) -> dict:
    """Apply cross-encoder reranking to the top fused results (ADR-0023 §9).

    Reranks the top RERANK_DEPTH candidates. If the reranker is unavailable
    or fails, preserves the RRF-fused order (fail-open).
    """
    # Cheap local check: is the reranker endpoint configured and running?
    # No network probe - rerank() itself fails open.
    available = modelserver.get_effective_reranker_endpoint() is not None

    if not available:
        result["reranker_state"] = "unavailable"
        return result

    matches = result["matches"]
    if not matches:
        result["reranker_state"] = "applied"
        return result

    # Wrap in try/except to preserve fail-open contract
    try:
        reranked = await reranker.rerank(query, matches)
    except Exception:
        log.warning("rerank failed; keeping RRF order", exc_info=True)
        result["reranker_state"] = "unavailable"
        return result

    if len(reranked) == len(matches):
        result["matches"] = reranked[:max_results]
        result["count"] = len(result["matches"])
        result["reranker_state"] = "applied"
    else:
        # Reranker returned different count; preserve original
        log.warning("reranker returned %d results for %d documents", len(reranked), len(matches))
        result["reranker_state"] = "unavailable"

    return result