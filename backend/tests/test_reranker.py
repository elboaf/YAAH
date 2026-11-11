"""Tests for the reranker client and search integration.

Tests the fail-open contract: reranker unavailable → original order
preserved. Tests the happy path with a mock reranker endpoint.
"""

import pytest
import httpx
from unittest.mock import patch, AsyncMock

from backend.agent import reranker
from backend.agent import modelserver


@pytest.mark.anyio
async def test_rerank_original_order_preserved_when_unavailable():
    """When reranker endpoint is unavailable, original order is preserved."""
    docs = [
        {"message_id": 1, "snippet": "first doc"},
        {"message_id": 2, "snippet": "second doc"},
        {"message_id": 3, "snippet": "third doc"},
    ]

    # Mock endpoint as None (reranker not running)
    result = await reranker.rerank("test query", docs, endpoint=None)

    # Original order preserved
    assert [d["message_id"] for d in result] == [1, 2, 3]


@pytest.mark.anyio
async def test_rerank_calls_endpoint():
    """Happy path: reranker endpoint returns scores, docs are re-ordered."""
    docs = [
        {"message_id": 1, "snippet": "first doc"},
        {"message_id": 2, "snippet": "second doc"},
        {"message_id": 3, "snippet": "third doc"},
    ]

    # Mock the internal _call_reranker function
    mock_response = {
        "results": [
            {"index": 2, "relevance_score": 0.95},
            {"index": 0, "relevance_score": 0.85},
            {"index": 1, "relevance_score": 0.70},
        ]
    }
    with patch.object(reranker, "_call_reranker", new_callable=AsyncMock, return_value=mock_response):
        result = await reranker.rerank("test query", docs, endpoint="http://localhost:9999/v1")

    # Docs reordered by relevance score
    assert [d["message_id"] for d in result] == [3, 1, 2]
    # Scores applied
    assert result[0]["rerank_score"] == 0.95
    assert result[1]["rerank_score"] == 0.85
    assert result[2]["rerank_score"] == 0.70


class _RaisingClient:
    """Fake AsyncClient that raises RequestError on any HTTP call."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        raise httpx.RequestError("timeout")

    async def get(self, *args, **kwargs):
        raise httpx.RequestError("connection refused")


@pytest.mark.anyio
async def test_rerank_endpoint_error_fails_open(monkeypatch):
    """Endpoint error → fail-open with original order."""
    docs = [
        {"message_id": 1, "snippet": "first doc"},
        {"message_id": 2, "snippet": "second doc"},
    ]

    monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)
    result = await reranker.rerank("test query", docs, endpoint="http://localhost:9999/v1")

    assert [d["message_id"] for d in result] == [1, 2]


@pytest.mark.anyio
async def test_rerank_empty_documents():
    """Empty document list returns empty list."""
    result = await reranker.rerank("query", [])
    assert result == []


@pytest.mark.anyio
async def test_reranker_is_available_when_server_running():
    """is_reranker_available returns True when endpoint is up."""
    with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = mock_get

        with patch("httpx.AsyncClient", return_value=mock_client):
            from backend.agent import modelserver
            with patch.object(modelserver, "get_effective_reranker_endpoint", return_value="http://localhost:9999/v1"):
                available = await reranker.is_reranker_available()
                assert available is True


@pytest.mark.anyio
async def test_reranker_not_available_when_server_down(monkeypatch):
    """is_reranker_available returns False when endpoint is down."""
    monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)
    with patch.object(modelserver, "get_effective_reranker_endpoint", return_value="http://localhost:9999/v1"):
        available = await reranker.is_reranker_available()
        assert available is False


def test_model_manifest_has_reranker():
    """The model manifest includes a reranker entry."""
    from backend.agent import modelserver
    reranker_models = modelserver.list_models("reranking")
    assert len(reranker_models) >= 1

    rm = reranker_models[0]
    assert rm["id"] == "qwen3-reranker-0.6b"
    assert "reranking" in rm["tasks"]
    assert "--reranking" in rm["flags"]
    assert "--pooling" in rm["flags"]
    assert "rank" in rm["flags"]


def test_default_model_for_reranking():
    """Default model for reranking task is set."""
    from backend.agent import modelserver
    m = modelserver.default_model("reranking")
    assert m["id"] == "qwen3-reranker-0.6b"


def test_tenant_separation():
    """Different tasks get different server managers."""
    from backend.agent import modelserver
    emb_mgr = modelserver.get_tenant("embedding")
    rer_mgr = modelserver.get_tenant("reranking")
    assert emb_mgr is not rer_mgr