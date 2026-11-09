"""Embeddings client: OpenAI-compatible /embeddings endpoint.

Phase 2a of conversation retrieval (issue #677). Model-agnostic: the
user configures an endpoint (api_base, api_key, model) in Settings --
typically a LAN llama.cpp server or similar. The client sends text
chunks and receives vectors; dimensions come from the response, never
hardcoded.

Degradation is the contract: on any endpoint failure callers get an
EmbeddingsError and fall back to keyword search. Nothing hard-fails.

Privacy: embed texts go to the configured endpoint and nowhere else --
the only network call in this module is the POST to {api_base}/embeddings.

Tests capture requests through the `_transport` seam (an httpx
AsyncBaseTransport) -- an input-capturing fake, never accept-anything:
the fake's handler records the actual request body for assertions.
"""

import json
import logging

import httpx

from backend.agent.config import load_config
from backend.agent import modelserver

log = logging.getLogger("yaah.embeddings")

EMBEDDINGS_TIMEOUT = httpx.Timeout(connect=10, read=60, write=10, pool=10)

# Module-level transport override (httpx AsyncBaseTransport). None in
# production -- every call opens a real AsyncClient. Tests set a
# MockTransport to capture requests; None restores real networking.
_transport: httpx.AsyncBaseTransport | None = None


class EmbeddingsError(Exception):
    """The embeddings endpoint failed (unreachable, bad request, timeout)."""


def _embeddings_config() -> dict:
    cfg = load_config()
    emb = cfg.get("embeddings") or {}
    return {
        "api_base": (emb.get("api_base") or "").strip(),
        "api_key": (emb.get("api_key") or "").strip(),
        "model": (emb.get("model") or "").strip(),
    }


async def embed_texts(texts: list[str]) -> tuple[list[list[float]], int, str]:
    """Embed a list of texts using the configured embeddings endpoint.

    Returns (embeddings, dimensions, model_name). embeddings[i] is the
    vector for texts[i]. dimensions is the vector size -- taken from the
    response, never hardcoded. model_name is the model that produced the
    embeddings (the response's echo when present, else the configured one).

    Raises EmbeddingsError on any failure. Callers should catch it and
    degrade to keyword-only search.
    """
    emb_cfg = _embeddings_config()
    # Local server takes precedence when it's running and healthy
    local_endpoint = modelserver.get_effective_embeddings_endpoint()
    if local_endpoint:
        api_base = local_endpoint
    else:
        api_base = emb_cfg["api_base"]
    api_key = emb_cfg["api_key"]
    model = emb_cfg["model"]

    if not api_base:
        raise EmbeddingsError("embeddings endpoint not configured")
    if not model:
        raise EmbeddingsError("embeddings model not configured")
    if not texts:
        raise EmbeddingsError("no texts to embed")

    url = api_base.rstrip("/") + "/embeddings"
    payload = {
        "model": model,
        "input": list(texts),
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    log.info(
        "embeddings request: base=%s model=%s count=%d",
        api_base, model, len(texts),
    )
    try:
        async with httpx.AsyncClient(timeout=EMBEDDINGS_TIMEOUT, transport=_transport) as client:
            r = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as e:
        raise EmbeddingsError(f"embeddings endpoint timed out: {api_base}") from e
    except httpx.RequestError as e:
        raise EmbeddingsError(f"embeddings endpoint unreachable: {api_base}") from e

    if r.status_code != 200:
        body = r.text[:500]
        raise EmbeddingsError(f"embeddings API error {r.status_code}: {body}")

    try:
        data = r.json()
    except json.JSONDecodeError as e:
        raise EmbeddingsError("embeddings endpoint returned non-JSON response") from e

    # OpenAI-compatible response:
    # {"data": [{"embedding": [...]}, ...], "model": "name", "usage": {...}}
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise EmbeddingsError("malformed embeddings response: missing data array")

    embeddings = []
    dimensions = None
    for item in data["data"]:
        emb = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(emb, list) or not emb:
            raise EmbeddingsError("malformed embeddings response: missing embedding field")
        if dimensions is None:
            dimensions = len(emb)
        elif len(emb) != dimensions:
            raise EmbeddingsError("inconsistent embedding dimensions from endpoint")
        embeddings.append(emb)

    if dimensions is None:
        raise EmbeddingsError("no embeddings returned")
    if len(embeddings) != len(texts):
        # A short response would silently drop chunks in the batch zip;
        # the build must know, not guess.
        raise EmbeddingsError(
            f"embeddings endpoint returned {len(embeddings)} vectors "
            f"for {len(texts)} inputs"
        )

    # The configured name is the identity stored with the index; the
    # response echo is advisory only.
    returned_model = data.get("model") or model

    log.info(
        "embeddings response: dimensions=%d model=%s count=%d",
        dimensions, returned_model, len(embeddings),
    )
    return embeddings, dimensions, returned_model


async def embed_text(text: str) -> tuple[list[float], int, str]:
    """Embed a single text (query path)."""
    embeddings, dimensions, model = await embed_texts([text])
    return embeddings[0], dimensions, model


async def check_embeddings_available() -> dict:
    """Probe the configured endpoint; never raises.

    Returns {configured: bool, available: bool, error?, dimensions?,
    model?}. `configured` reports the config block state; available=False
    with configured=True means configured but unreachable/misbehaving --
    the degraded-mode signal.
    """
    emb_cfg = _embeddings_config()
    configured = bool(emb_cfg["api_base"]) and bool(emb_cfg["model"])
    if not configured:
        return {"configured": False, "available": False, "error": "not configured"}

    try:
        _, dimensions, returned_model = await embed_text("test")
        return {
            "configured": True,
            "available": True,
            "dimensions": dimensions,
            "model": returned_model,
        }
    except EmbeddingsError as e:
        return {"configured": True, "available": False, "error": str(e)}
