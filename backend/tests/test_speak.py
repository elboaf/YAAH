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


# ---- emoji stripping ----

def test_strip_emoji_removes_and_repairs_spacing():
    """espeak-ng speaks emoji NAMES ('waving hand sign', ~1s each); emoji
    must go, but the punctuation and words around them must survive."""
    assert speak.strip_emoji("Take your time 🙂 whenever you're ready.") == (
        "Take your time whenever you're ready."
    )
    assert speak.strip_emoji("Loud and clear! 👋 I'm here.") == "Loud and clear! I'm here."
    assert speak.strip_emoji("Done ✅") == "Done"


def test_strip_emoji_handles_sequences_and_flags():
    # ZWJ sequences (👨‍👩‍👧 = three emoji joined by ZWJ) and flags collapse
    # to nothing, variation selectors too.
    assert speak.strip_emoji("family: \U0001F468\u200D\U0001F469\u200D\U0001F467 end") == "family: end"
    assert speak.strip_emoji("flag \U0001F1FA\U0001F1F8 done") == "flag done"
    assert speak.strip_emoji("thumb \U0001F44D\uFE0F up") == "thumb up"


def test_strip_emoji_keeps_arrows_and_text():
    """Arrows are legitimate technical prose ('A → B') and must survive."""
    t = "A → B, and \u2192 stays; plain words unaffected."
    assert speak.strip_emoji(t) == t
    assert "workspace" in speak.strip_emoji("the workspace")


def test_prose_for_speech_strips_emoji():
    out = speak.prose_for_speech("Happy to chat! Not much going on 😄 just waiting.")
    assert "😄" not in out
    assert "going on just waiting" in out


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


@pytest.mark.asyncio
async def test_tts_stop_accepts_floor(tts_env, monkeypatch):
    """stop(floor=N) raises the supersede floor exactly to N; the real-engine
    semantics this protects are covered in test_synthesize_*."""
    from backend.agent import speak

    seen = {}
    monkeypatch.setattr(speak, "ensure_epoch", lambda floor: seen.setdefault("floor", floor))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/tts/stop", json={"floor": 42})
    assert res.status_code == 200
    assert seen["floor"] == 42


# ---- replace semantics (supersede floor) ----

@pytest.fixture
def clean_floor(monkeypatch):
    """Isolate the module-level supersede floor per test."""
    monkeypatch.setattr(speak, "_floor", 0)


def test_synthesize_superseded_mid_generation(tts_env, monkeypatch, clean_floor):
    """A stop (floor raised past the utterance id) during generation aborts
    the stale chunk instead of letting it hold the synth lock. NOTE sherpa's
    callback convention: return 1 = continue, 0 = stop (probe-verified —
    inverted from what you'd guess; getting it backwards truncated every
    chunk to its first sentence)."""
    import types

    MINE = 100

    class FakeTts:
        sample_rate = 24000
        num_speakers = 54

        def generate(self, text, sid=0, speed=1.0, callback=None):
            if callback:
                assert callback([0.0], 0.5) == 1  # still current -> continue
            speak.ensure_epoch(MINE + 1)  # replacement utterance stops this one
            if callback:
                assert callback([0.0], 1.0) == 0  # now stale -> abort
            return types.SimpleNamespace(samples=[0.0, 0.5, -0.5], sample_rate=24000)

    monkeypatch.setattr(speak, "_engine", FakeTts())
    with pytest.raises(speak.SupersededError):
        speak.synthesize("hello", epoch=MINE)


def test_synthesize_same_epoch_prefetch_survives(tts_env, monkeypatch, clean_floor):
    """THE regression that silenced chat narration: concurrent chunks of one
    utterance share an epoch, so fetching chunk N+1 must never abort chunk N."""
    import types

    class FakeTts:
        sample_rate = 24000
        num_speakers = 54

        def generate(self, text, sid=0, speed=1.0, callback=None):
            if callback:
                assert callback([0.0], 0.5) == 1
            return types.SimpleNamespace(samples=[0.0, 0.5, -0.5], sample_rate=24000)

    monkeypatch.setattr(speak, "_engine", FakeTts())
    # Chunk 0 in flight, chunk 1 arrives with the SAME epoch: both succeed.
    first = speak.synthesize("chunk zero", epoch=7)
    second = speak.synthesize("chunk one", epoch=7)
    assert len(first[0]) == 6 and len(second[0]) == 6


def test_ensure_epoch_floor_never_lowers(tts_env):
    assert speak.ensure_epoch(5) == 5
    assert speak.ensure_epoch(2) == 5  # ignored
    assert speak.superseded(5) and speak.superseded(4)
    assert not speak.superseded(6)


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
        epoch=1,  # floor starts at 0, so epoch 1 is current
    )
    assert rate == 24000
    assert len(pcm) > 24000  # at least a second of 16-bit mono audio
