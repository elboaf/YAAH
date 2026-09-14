"""TTS read-aloud: model resolution, prose extraction, chunking, endpoints.

The real engine is not exercised here (no model in CI); these tests stub
`synthesize` and the model-dir resolver, mirroring how test_voice.py fakes
the whisper binary.
"""
import os

import pytest
from httpx import ASGITransport, AsyncClient

from backend.agent import speak
from backend.main import app


@pytest.fixture
def tts_env(tmp_path, monkeypatch):
    """Fake model dir: the resolver only checks for the file set."""
    d = tmp_path / "kokoro-int8-multi-lang-v1_0"
    d.mkdir()
    (d / "model.int8.onnx").write_text("stub")
    (d / "voices.bin").write_text("stub")
    (d / "tokens.txt").write_text("stub")
    (d / "espeak-ng-data").mkdir()
    monkeypatch.setenv("YAAH_TTS_MODEL_DIR", str(d))
    return d


# ---- voice table ----

def test_voice_table_is_authoritative():
    """54 voices, English ones first-class; the picker list must never
    silently drift from the model's voices.bin order."""
    assert len(speak.VOICES) == 54
    assert speak.DEFAULT_VOICE in speak.ENGLISH_VOICES
    assert speak.VOICES.index(speak.DEFAULT_VOICE) == 3  # af_heart -> sid 3
    assert speak.ENGLISH_VOICES[0] == "af_alloy"
    assert "em_santa" in speak.VOICES  # the voice the docs page predates


def test_voice_id_falls_back_to_default():
    assert speak.voice_id("af_heart") == 3
    assert speak.voice_id("nonexistent") == speak.voice_id(speak.DEFAULT_VOICE)


# ---- prose extraction ----

def test_prose_drops_code_blocks_keeps_text():
    md = (
        "Here is the fix:\n\n```python\nprint('never read aloud')\n```\n\n"
        "It prints the greeting now."
    )
    out = speak.prose_for_speech(md)
    assert "never read aloud" not in out
    assert "It prints the greeting now." in out


def test_prose_keeps_link_labels_strips_emphasis():
    md = "See [the docs](https://example.com) for **details** and `config.json`."
    out = speak.prose_for_speech(md)
    assert "the docs" in out
    assert "https://example.com" not in out
    assert "details" in out
    assert "**" not in out
    assert "`" not in out


def test_prose_drops_tables_and_images():
    md = "Before:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n![pic](x.png)\n\nAfter."
    out = speak.prose_for_speech(md)
    assert "|" not in out
    assert "pic" not in out
    assert "Before:" in out and "After." in out


def test_prose_truncates_long_text_at_sentence():
    md = "One. " + ("A sentence about the workspace. " * 200)
    out = speak.prose_for_speech(md, max_chars=500)
    assert len(out) <= 520
    assert out.endswith((".", "!", "?"))


# ---- sentence chunking ----

def test_split_sentences_merges_short_and_respects_abbrevs():
    text = (
        "This is a short one. And another! A third? "
        "The value is e.g. config.json per Dr. Smith, which continues here. "
        "Final sentence that is long enough to stand on its own two feet."
    )
    chunks = speak.split_sentences(text)
    joined = " ".join(chunks)
    assert "e.g. config.json per Dr. Smith, which continues here" in chunks[0] or all(
        "e.g" not in c.split("Dr.")[0][-6:] for c in chunks
    )
    # No chunk lost content
    assert "Final sentence" in joined
    assert all(len(c) <= 320 for c in chunks)


def test_split_sentences_hard_splits_monsters():
    text = ("word " * 200).strip() + "."
    chunks = speak.split_sentences(text)
    assert all(len(c) <= 320 for c in chunks)
    assert "".join(c.replace(" ", "") for c in chunks).startswith("wordword")


# ---- endpoints ----

@pytest.mark.asyncio
async def test_tts_status_endpoint(tts_env):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/tts/status")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert body["default_voice"] == "af_heart"
    assert "af_heart" in body["voices"]
    assert body["model_bytes"] > 100_000_000


@pytest.mark.asyncio
async def test_tts_synthesize_returns_wav(tts_env, monkeypatch):
    import io
    import wave

    def fake_synth(text, voice="af_heart", speed=1.0, epoch=None):
        pcm = b"\x00\x01" * 2400  # 100 ms of 24 kHz
        return pcm, 24000

    monkeypatch.setattr(speak, "synthesize", fake_synth)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/tts/synthesize", json={"text": "Hello there."})
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(res.content), "rb") as w:
        assert w.getframerate() == 24000
        assert w.getnchannels() == 1


@pytest.mark.asyncio
async def test_tts_synthesize_409_without_model(monkeypatch):
    monkeypatch.setattr(speak, "model_available", lambda: False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/tts/synthesize", json={"text": "Hello"})
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_tts_synthesize_rejects_empty_and_huge(tts_env):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        empty = await client.post("/api/tts/synthesize", json={"text": "  "})
        huge = await client.post("/api/tts/synthesize", json={"text": "x" * 6000})
    assert empty.status_code == 400
    assert huge.status_code == 413


@pytest.mark.asyncio
async def test_tts_settings_roundtrip(tts_env):
    """tts_enabled / tts_voice / tts_speed persist through the masked
    config GET/PUT cycle, like the dictation settings do."""
    from backend.agent.config import load_config

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.put(
            "/api/config",
            json={"voice": {"tts_enabled": True, "tts_voice": "bm_george", "tts_speed": 1.25}},
        )
        assert res.status_code == 200
        got = (await client.get("/api/config")).json()
    assert got["voice"]["tts_enabled"] is True
    assert got["voice"]["tts_voice"] == "bm_george"
    assert got["voice"]["tts_speed"] == 1.25
    assert load_config()["voice"]["tts_voice"] == "bm_george"


@pytest.mark.asyncio
async def test_tts_download_refuses_concurrent(tts_env, monkeypatch):
    monkeypatch.setattr(speak, "download_in_progress", lambda: True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/tts/download")
    assert res.status_code == 409


# ---- replace semantics (synthesis epoch) ----

def test_synthesize_superseded_mid_generation(tts_env, monkeypatch):
    """A stop (epoch bump) during generation aborts the stale chunk instead
    of letting it hold the synth lock for a full chunk."""
    import types

    class FakeTts:
        sample_rate = 24000
        num_speakers = 54

        def generate(self, text, sid=0, speed=1.0, callback=None):
            if callback:
                callback([0.0], 0.5)  # first internal sentence: still current
            speak.bump_epoch()  # replacement utterance stops this one
            if callback:
                callback([0.0], 1.0)  # second: now stale -> callback returns 1
            return types.SimpleNamespace(samples=[0.0, 0.5, -0.5], sample_rate=24000)

    monkeypatch.setattr(speak, "_engine", FakeTts())
    mine = speak.bump_epoch()
    with pytest.raises(speak.SupersededError):
        speak.synthesize("hello", epoch=mine)


def test_synthesize_current_epoch_succeeds(tts_env, monkeypatch):
    import types

    class FakeTts:
        sample_rate = 24000
        num_speakers = 54

        def generate(self, text, sid=0, speed=1.0, callback=None):
            assert callback is not None and callback([0.0], 0.5) == 0
            return types.SimpleNamespace(samples=[0.0, 0.5, -0.5], sample_rate=24000)

    monkeypatch.setattr(speak, "_engine", FakeTts())
    mine = speak.bump_epoch()
    pcm, rate = speak.synthesize("hello", epoch=mine)
    assert len(pcm) == 6 and rate == 24000


@pytest.mark.asyncio
async def test_tts_synthesize_superseded_maps_to_409(tts_env, monkeypatch):
    def fake_synth(text, voice="af_heart", speed=1.0, epoch=None):
        raise speak.SupersededError("superseded")

    monkeypatch.setattr(speak, "synthesize", fake_synth)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/tts/synthesize", json={"text": "Hello"})
    assert res.status_code == 409


# ---- real-engine integration (regression: lazy numpy import) ----

@pytest.mark.skipif(
    not os.environ.get("YAAH_TTS_MODEL_DIR"),
    reason="real Kokoro model not available (CI)",
)
def test_real_engine_synthesizes_with_abort_callback():
    """Guards the lazy `import numpy` inside sherpa's generate(callback=...):
    the callback is marshaled through numpy, so the frozen sidecar must
    bundle numpy even though sherpa only imports it on this path (PyInstaller
    cannot see it). Multi-sentence text so the callback actually fires
    between sentences. Ran with numpy absent once — it 500'd everywhere."""
    if speak._engine is None:
        speak.get_engine()
    assert speak._engine is not None
    pcm, rate = speak.synthesize(
        "First sentence here. Second sentence follows. Third and final one.",
        voice=speak.DEFAULT_VOICE,
        speed=1.0,
        epoch=speak.bump_epoch(),
    )
    assert rate == 24000
    assert len(pcm) > 24000  # at least a second of 16-bit mono audio
