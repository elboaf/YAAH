"""Reranker client for cross-encoder post-retrieval reranking.

Uses a local llama-server process running with --reranking --pooling rank
(ADR-0023 §9). This is a second tenant separate from the embeddings
server, as --reranking and --embeddings are mutually exclusive.

The client calls POST /v1/rerank with a query and list of documents,
and returns relevance scores. On any error, it degrades gracefully by
returning the original ranking (fail-open contract).
"""

import logging
from typing import Optional

import httpx

from backend.agent import modelserver

log = logging.getLogger("yaah.reranker")

# Reranker depth: how many top fused results to rerank
RERANK_DEPTH = 20

# Timeout for reranker requests (first call after cold start can be slow)
RERANK_TIMEOUT = 30.0


class RerankerError(Exception):
    """Error calling the reranker endpoint."""


async def rerank(
    query: str,
    documents: list[dict],
    endpoint: Optional[str] = None,
) -> list[dict]:
    """Rerank documents by relevance to query using cross-encoder model.

    Args:
        query: The search query.
        documents: List of result dicts, each with 'message_id' and 'snippet'
                  (or 'text') fields.
        endpoint: Reranker /v1 base URL. If None, uses the local model server.

    Returns:
        Documents re-ranked by relevance score. Each doc gains a 'rerank_score'
        field. On error, returns the original list unchanged (fail-open).
    """
    if not endpoint:
        endpoint = modelserver.get_effective_reranker_endpoint()
    if not endpoint:
        log.debug("reranker not available; returning original ranking")
        return documents

    # Extract text content for reranking
    docs_text = []
    for doc in documents:
        text = doc.get("snippet") or doc.get("text") or ""
        if not text:
            text = f"[message {doc.get('message_id', 'unknown')}]"
        docs_text.append(text)

    try:
        response = await _call_reranker(endpoint, query, docs_text)
    except Exception as e:
        log.warning("reranker failed, falling back to original ranking: %s", e)
        return documents

    # Build score lookup by index
    score_by_index = {}
    for result in response.get("results", []):
        idx = result.get("index")
        score = result.get("relevance_score", 0.0)
        if idx is not None:
            score_by_index[idx] = score

    # Validate: require scores for all documents
    if set(score_by_index) != set(range(len(documents))):
        log.warning("reranker returned incomplete scores; keeping original ranking")
        return documents

    # Apply scores and re-sort
    for i, doc in enumerate(documents):
        doc["rerank_score"] = score_by_index[i]

    return sorted(documents, key=lambda d: d.get("rerank_score", 0.0), reverse=True)


async def _call_reranker(
    endpoint: str,
    query: str,
    documents: list[str],
) -> dict:
    """Call the reranker endpoint."""
    url = f"{endpoint}/rerank"
    payload = {
        "query": query,
        "documents": documents,
        "top_n": len(documents),
    }

    timeout = httpx.Timeout(RERANK_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload)

    if response.status_code not in (200, 201):
        raise RerankerError(
            f"reranker returned {response.status_code}: {response.text[:200]}"
        )

    return response.json()


async def is_reranker_available() -> bool:
    """Check if a reranker endpoint is available and healthy."""
    endpoint = modelserver.get_effective_reranker_endpoint()
    if not endpoint:
        return False

    try:
        timeout = httpx.Timeout(5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{endpoint}/models")
        return response.status_code == 200
    except Exception:
        return False