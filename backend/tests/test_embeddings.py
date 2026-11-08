"""Embeddings client tests (issue #677, Phase 2a).

Input-capturing fakes only: every fake records the ACTUAL request body
via the _transport seam, so assertions run against what was really
sent (model name, input list, auth header) -- never accept-anything.
Dimensions come from the fake's response and must be read back, never
assumed.
"""
import json

import httpx
import pytest

from backend.agent import config as config_mod
from backend.agent import embeddings
from backend.agent.embeddings import EmbeddingsError

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Per-test config file: no test reads or writes the real one."""
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")


class _CapturingTransport(httpx.MockTransport):
    """Records each request before the handler answers it. The async
    path bypasses handle_request, so override handle_async_request."""

    def __init__(self, handler):
        super().__init__(handler)
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content.decode("utf-8")))
        return self.handler(request)


def _embedding_response(vectors: list[list[float]], model: str = "bge-m3") -> bytes:
    return json.dumps({
        "data": [{"embedding": v} for v in vectors],
        "model": model,
    }).encode("utf-8")


def _install(monkeypatch, vectors, model="bge-m3", capture=None):
    """Install a capturing transport returning `vectors`."""
    transport = _CapturingTransport(
        lambda request: httpx.Response(200, content=_embedding_response(vectors, model))
    )
    monkeypatch.setattr(embeddings, "_transport", transport)
    return transport


async def test_embed_texts_sends_configured_model_and_input(monkeypatch):
    """The request body names the CONFIGURED model and carries the exact
    input texts -- captured from the wire, not inferred."""
    config_mod.save_config({"embeddings": {
        "api_base": "http://127.0.0.1:8099",
        "api_key": "sekret",
        "model": "bge-m3",
    }})
    vec = [[0.1] * 8]
    transport = _install(monkeypatch, vec)

    out, dims, model = await embeddings.embed_texts(["hello world"])

    assert len(transport.requests) == 1
    req = transport.requests[0]
    body = transport.bodies[0]
    assert str(req.url) == "http://127.0.0.1:8099/embeddings"
    assert req.headers["authorization"] == "Bearer sekret"
    # Model from CONFIG in the request body; dimensions from RESPONSE.
    assert body["model"] == "bge-m3"
    assert body["input"] == ["hello world"]
    assert out == [[0.1] * 8]
    assert dims == 8
    assert model == "bge-m3"


async def test_dimensions_come_from_response_not_config(monkeypatch):
    """Whatever the endpoint returns IS the dimensionality -- config
    carries no dimension field and none is invented."""
    config_mod.save_config({"embeddings": {
        "api_base": "http://x", "api_key": "", "model": "m",
    }})
    vec = [[0.5] * 1024]
    _install(monkeypatch, vec, model="m")
    _, dims, _ = await embeddings.embed_texts(["q"])
    assert dims == 1024


async def test_batch_request_carries_all_texts(monkeypatch):
    config_mod.save_config({"embeddings": {
        "api_base": "http://x", "api_key": "", "model": "m",
    }})
    vectors = [[0.1] * 4, [0.2] * 4, [0.3] * 4]
    transport = _install(monkeypatch, vectors, model="m")
    out, dims, _ = await embeddings.embed_texts(["a", "b", "c"])
    assert transport.bodies[0]["input"] == ["a", "b", "c"]
    assert out == vectors
    assert dims == 4


async def test_unreachable_endpoint_raises_embeddings_error(monkeypatch):
    """Connection failure -> EmbeddingsError (callers degrade), and the
    capture proves the request actually went out."""
    config_mod.save_config({"embeddings": {
        "api_base": "http://x", "api_key": "", "model": "m",
    }})

    def unreachable(request):
        raise httpx.ConnectError("no route to host", request=request)

    transport = _CapturingTransport(unreachable)
    monkeypatch.setattr(embeddings, "_transport", transport)
    with pytest.raises(EmbeddingsError, match="unreachable"):
        await embeddings.embed_texts(["q"])
    assert len(transport.requests) == 1  # the attempt happened


async def test_not_configured_raises_without_network(monkeypatch):
    """No api_base/model -> EmbeddingsError and NO request at all."""
    transport = _install(monkeypatch, [[0.1]])
    with pytest.raises(EmbeddingsError, match="not configured"):
        await embeddings.embed_texts(["q"])
    assert transport.requests == []


async def test_http_error_surfaces_status(monkeypatch):
    config_mod.save_config({"embeddings": {
        "api_base": "http://x", "api_key": "", "model": "m",
    }})
    transport = _CapturingTransport(
        lambda request: httpx.Response(500, content=b"boom")
    )
    monkeypatch.setattr(embeddings, "_transport", transport)
    with pytest.raises(EmbeddingsError, match="500"):
        await embeddings.embed_texts(["q"])


async def test_malformed_response_rejected(monkeypatch):
    config_mod.save_config({"embeddings": {
        "api_base": "http://x", "api_key": "", "model": "m",
    }})
    for bad in (
        b'{"nope": true}',
        b'{"data": [{"not_embedding": 1}]}',
        b'{"data": []}',
        b'not json at all',
    ):
        transport = _CapturingTransport(lambda request, b=bad: httpx.Response(200, content=b))
        monkeypatch.setattr(embeddings, "_transport", transport)
        with pytest.raises(EmbeddingsError):
            await embeddings.embed_texts(["q"])


async def test_inconsistent_dimensions_rejected(monkeypatch):
    config_mod.save_config({"embeddings": {
        "api_base": "http://x", "api_key": "", "model": "m",
    }})
    transport = _CapturingTransport(
        lambda request: httpx.Response(
            200, content=_embedding_response([[0.1] * 4, [0.2] * 8], model="m")
        )
    )
    monkeypatch.setattr(embeddings, "_transport", transport)
    with pytest.raises(EmbeddingsError, match="inconsistent"):
        await embeddings.embed_texts(["a", "b"])


async def test_no_auth_header_when_key_blank(monkeypatch):
    config_mod.save_config({"embeddings": {
        "api_base": "http://x", "api_key": "", "model": "m",
    }})
    transport = _install(monkeypatch, [[0.1] * 2], model="m")
    await embeddings.embed_texts(["q"])
    assert "authorization" not in transport.requests[0].headers


async def test_check_available_reports_unreachable(monkeypatch):
    """Probe never raises: configured-but-down -> available=False."""
    config_mod.save_config({"embeddings": {
        "api_base": "http://x", "api_key": "", "model": "m",
    }})
    transport = _CapturingTransport(
        lambda request: (_ for _ in ()).throw(
            httpx.ConnectError("down", request=request))
    )
    monkeypatch.setattr(embeddings, "_transport", transport)
    status = await embeddings.check_embeddings_available()
    assert status == {
        "configured": True,
        "available": False,
        "error": "embeddings endpoint unreachable: http://x",
    }
    assert len(transport.requests) == 1


async def test_check_available_not_configured(monkeypatch):
    status = await embeddings.check_embeddings_available()
    assert status["configured"] is False
    assert status["available"] is False


async def test_transcript_text_goes_only_to_configured_endpoint(monkeypatch):
    """Privacy contract: the captured request URL is the configured
    endpoint and nothing else is contacted (one request, that URL)."""
    config_mod.save_config({"embeddings": {
        "api_base": "http://embed-host:1234/v1",
        "api_key": "k",
        "model": "m",
    }})
    transport = _install(monkeypatch, [[0.1] * 3], model="m")
    await embeddings.embed_texts(["private transcript words"])
    assert len(transport.requests) == 1
    assert str(transport.requests[0].url) == "http://embed-host:1234/v1/embeddings"
