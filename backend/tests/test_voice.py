"""Voice transcription: local engine resolution, endpoint, key masking."""
import wave

import pytest
from httpx import ASGITransport, AsyncClient

from backend.agent import transcribe
from backend.main import app


@pytest.fixture
def whisper_env(tmp_path, monkeypatch):
    """Fake local engine: a stub binary + model the resolver can find."""
    bindir = tmp_path / "whisper" / "bin"
    models = tmp_path / "whisper" / "models"
    bindir.mkdir(parents=True)
    models.mkdir(parents=True)
    (bindir / "whisper-cli.exe").write_text("stub")
    (models / "ggml-base-q5_1.bin").write_text("stub")
    monkeypatch.setenv("YAAH_WHISPER_BIN", str(bindir))
    monkeypatch.setenv("YAAH_WHISPER_MODEL", str(models / "ggml-base-q5_1.bin"))
    return bindir


def test_resolver_prefers_bundled_names(whisper_env):
    assert transcribe.local_available()
    assert transcribe.find_model().name == "ggml-base-q5_1.bin"


def test_save_wav_wraps_pcm():
    import io

    pcm = b"\x00\x01" * 1600  # 100 frames of 16-bit mono
    path = transcribe.save_wav(pcm)
    with wave.open(path, "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == 16000
        assert w.getnframes() == 1600
    import os

    os.unlink(path)


def test_save_wav_rejects_empty():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        transcribe.save_wav(b"")


@pytest.mark.asyncio
async def test_transcribe_endpoint_local(monkeypatch):
    def fake_local(wav_path):
        return "hello from stub"

    monkeypatch.setattr(transcribe, "transcribe_local", fake_local)
    monkeypatch.setattr(transcribe, "local_available", lambda: True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post(
            "/api/transcribe",
            content=b"\x00\x01" * 3200,
            headers={"Content-Type": "audio/wav"},
        )
    assert res.status_code == 200
    assert res.json() == {"text": "hello from stub"}


@pytest.mark.asyncio
async def test_transcribe_endpoint_rejects_empty():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post("/api/transcribe", content=b"")
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_cloud_key_survives_config_roundtrip(tmp_path, monkeypatch):
    """PUT with the masked 'set' value must not overwrite the stored key."""
    from backend.agent.config import load_config, save_config

    save_config({"voice": {"engine": "cloud", "cloud_endpoint": "https://x/v1",
                           "cloud_api_key": "sk-real", "cloud_model": "whisper-1"}})
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        got = (await client.get("/api/config")).json()
        assert got["voice"]["cloud_api_key"] == "set"  # masked, never the key
        res = await client.put("/api/config", json={"voice": {"engine": "cloud",
                                                              "cloud_api_key": "set"}})
        assert res.status_code == 200
    assert load_config()["voice"]["cloud_api_key"] == "sk-real"


@pytest.mark.asyncio
async def test_cloud_optional_key_and_inference_endpoint(tmp_path, monkeypatch):
    """whisper.cpp server style: /inference URL kept as-is, no key, no model."""
    import httpx

    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"text": " hello "})

    real_client = httpx.AsyncClient
    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    wav = transcribe.save_wav(b"\x00\x01" * 160)
    try:
        text = await transcribe.transcribe_cloud(wav, "http://192.168.1.10:8080/inference", "", "")
    finally:
        import os
        os.unlink(wav)
    assert text == "hello"
    assert captured["url"] == "http://192.168.1.10:8080/inference"
    assert captured["auth"] is None
