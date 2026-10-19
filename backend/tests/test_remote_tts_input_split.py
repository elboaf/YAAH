# -*- coding: utf-8 -*-
"""Adaptive input splitting for the remote TTS engine.

The speech contract caps `input` wherever each server pleases and only
admits it in its own rejection: OpenAI documents 4096 chars; the
MOSS-TTS-Realtime wrapper rejects anything over 48 (its model tops out at
~41 s of audio per request). The client learns the cap from a "too long"
4xx, re-splits at sentence boundaries, synthesizes the pieces in order,
and merges them into one WAV so the #205 response contract is unchanged.
The learned cap is cached per origin. Verified against a fake transport,
never the network.
"""

from __future__ import annotations

import httpx
import pytest

from backend.agent import speak


# The REAL client class, captured before any test patches speak.httpx.
_RealAsyncClient = httpx.AsyncClient

MOSS_DETAIL = (
    "HTTP 400: input too long: this model tops out at ~41 s of audio per "
    "request (48 chars max); split longer text client-side"
)


@pytest.fixture(autouse=True)
def _clean_caps():
    """The learned-cap cache is module state: reset around every test so
    order-independence holds (the #317 clean_floor precedent)."""
    speak._REMOTE_CAPS.clear()
    yield
    speak._REMOTE_CAPS.clear()


class _Recorder(httpx.MockTransport):
    """Captures each request body; handler returns per-request responses."""

    def __init__(self, handler):
        super().__init__(handler)
        self.bodies: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        import json

        self.bodies.append(json.loads(request.content.decode("utf-8")))
        return self.handler(request)


def _install(monkeypatch, transport) -> None:
    monkeypatch.setattr(
        speak.httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=transport)
    )


def _wav(pcm: bytes, rate: int = 24000) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _pcm(tag: int, frames: int = 240) -> bytes:
    """Distinct audio content per call: (tag, tag+1) sample pairs."""
    out = bytearray()
    for _ in range(frames):
        out += bytes([tag & 0xFF, (tag + 1) & 0xFF])
    return bytes(out)


LONG = (
    "Adaptive narration needs sentences that survive a stubborn server. "
    "The wrapper only wants forty-eight characters at a time. "
    "So the client learns that limit and splits. "
    "Every piece comes back and plays as one buffer. "
    "The queue keeps its prefetch, and the text stays visible."
)


# ---- 1. learning: one rejection, then sentence-bounded re-split ------------


async def test_oversized_input_learns_cap_and_returns_one_wav(monkeypatch):
    """A 'too long' 400 with a stated cap: exactly one rejection, pieces
    re-split at the server's own number, one merged WAV out."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        n = len(json.loads(request.content.decode("utf-8"))["input"])
        calls.append(n)
        if n > 48:
            return httpx.Response(400, json={"detail": MOSS_DETAIL})
        return httpx.Response(200, content=_wav(_pcm(n)))

    _install(monkeypatch, _Recorder(handler))

    audio = await speak.synthesize_remote(
        LONG,
        endpoint="http://moss.local:9800/v1/audio/speech",
        api_key="",
        model="moss",
        voice="default",
        speed=None,
    )

    assert len(calls) > 2  # 1 rejection + several pieces
    assert calls[0] == len(LONG)  # the first attempt was the verbatim chunk
    assert sum(1 for n in calls if n > 48) == 1  # exactly one rejection
    assert max(calls[1:]) <= 48  # no piece over the stated cap

    # The merged bytes are ONE valid WAV, decodable by the stdlib reader.
    import io
    import wave

    with wave.open(io.BytesIO(audio), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000)
        raw = w.readframes(w.getnframes())
    assert speak._REMOTE_CAPS == {"http://moss.local:9800": 48}
    # Every piece's content made it into the merged stream (order-checked
    # by test_merge_wav_concatenates_frames; here, total size is the sum).
    assert len(raw) == sum(len(_pcm(n)) for n in calls[1:])


async def test_cap_is_cached_per_origin(monkeypatch):
    """The second oversized chunk to the SAME server pre-splits (no new
    rejection); a DIFFERENT server still learns its own cap."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        n = len(json.loads(request.content.decode("utf-8"))["input"])
        seen.append(n)
        if n > 48:
            return httpx.Response(400, json={"detail": MOSS_DETAIL})
        return httpx.Response(200, content=_wav(_pcm(1)))

    _install(monkeypatch, _Recorder(handler))

    for endpoint in (
        "http://moss.local:9800",
        "http://moss.local:9800",
        "http://other.local:9800",
    ):
        await speak.synthesize_remote(
            LONG, endpoint=endpoint, api_key="", model="moss", voice="d", speed=None
        )

    assert seen.count(len(LONG)) == 2  # first contact each for moss + other
    assert speak._REMOTE_CAPS == {
        "http://moss.local:9800": 48,
        "http://other.local:9800": 48,
    }


# ---- 2. misfits: non-length 4xx, un-numbered rejections, split limits -----


async def test_non_length_4xx_is_final_and_never_split(monkeypatch):
    """A bad-voice 404 is the server's final answer: no split, no retry,
    verbatim detail. (_remote_error_detail unwraps FastAPI detail.)"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Voice zzz not found"})

    _install(monkeypatch, _Recorder(handler))

    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_remote(
            LONG, endpoint="http://x.local", api_key="", model="m", voice="zzz", speed=None
        )
    assert str(ei.value) == "HTTP 404: Voice zzz not found"
    assert speak._REMOTE_CAPS == {}


async def test_unnumbered_too_long_halves_and_splits(monkeypatch):
    """A 'too long' 4xx with no stated number: halve the failed input and
    split at sentence boundaries. (YAAH's own synthesize endpoint answers
    'text too long' this way at 5000 chars, so chained YAAH pairs work.)"""
    seen: list[tuple[int, int]] = []  # (input length, status)

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        n = len(json.loads(request.content.decode("utf-8"))["input"])
        if n > 60:
            seen.append((n, 413))
            return httpx.Response(413, json={"detail": "HTTP 413: text too long"})
        seen.append((n, 200))
        return httpx.Response(200, content=_wav(_pcm(n)))

    _install(monkeypatch, _Recorder(handler))

    audio = await speak.synthesize_remote(
        LONG, endpoint="http://x.local", api_key="", model="m", voice="v", speed=None
    )
    # The invariant: nothing ever SUCCEEDS over the server's limit (the
    # first verbatim attempt and intermediate guesses may be rejected).
    assert {n for n, status in seen if status == 200} and max(
        n for n, status in seen if status == 200
    ) <= 60
    # Iterated halving converges; the cached cap is the smallest bound the
    # server tolerated (273 -> 136 -> 61 -> 33 in this trace).
    assert speak._REMOTE_CAPS["http://x.local"] <= 60
    assert audio.startswith(b"RIFF")


async def test_unbroken_text_still_gets_rescued(monkeypatch):
    """Even one unbroken 120-char run: split_sentences hard word-splits
    monsters, so the chunk is rescued instead of dropped."""
    import json as _json

    text = "A" * 120 + "."

    def handler(request: httpx.Request) -> httpx.Response:
        if len(_json.loads(request.content.decode("utf-8"))["input"]) > 48:
            return httpx.Response(400, json={"detail": MOSS_DETAIL})
        return httpx.Response(200, content=_wav(_pcm(1)))

    _install(monkeypatch, _Recorder(handler))

    audio = await speak.synthesize_remote(
        text, endpoint="http://x.local", api_key="", model="m", voice="v", speed=None
    )
    assert audio.startswith(b"RIFF")
    assert speak._REMOTE_CAPS == {"http://x.local": 48}


async def test_cap_guarantees_nothing_when_server_lies(monkeypatch):
    """A learned cap is advisory, not a contract: if the server still
    refuses a piece, the server's answer surfaces and the whole chunk
    drops - no partial audio, no infinite shrink loop."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        n = len(json.loads(request.content.decode("utf-8"))["input"])
        calls.append(n)
        if n > 48:
            return httpx.Response(400, json={"detail": MOSS_DETAIL})
        if n == 10:  # a piece inside the cap still fails (deterministic
            return httpx.Response(500, json={"detail": "GPU went away"})  # by content)
        return httpx.Response(200, content=_wav(_pcm(1)))

    _install(monkeypatch, _Recorder(handler))

    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_remote(
            LONG, endpoint="http://x.local", api_key="", model="m", voice="v", speed=None
        )
    assert "GPU went away" in str(ei.value)
    assert speak._REMOTE_CAPS == {"http://x.local": 48}  # learned before the drop


# ---- 3. merging: format guard + the stdlib merger itself -------------------


async def test_merge_raises_when_pieces_disagree_on_format(monkeypatch):
    """A server answering pieces in different formats is broken: the merge
    surfaces it instead of gluing garbage audio."""

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        n = len(json.loads(request.content.decode("utf-8"))["input"])
        if n > 48:
            return httpx.Response(400, json={"detail": MOSS_DETAIL})
        rate = 24000 if n > 20 else 16000  # pieces disagree on rate
        return httpx.Response(200, content=_wav(_pcm(1), rate=rate))

    _install(monkeypatch, _Recorder(handler))

    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_remote(
            LONG, endpoint="http://x.local", api_key="", model="m", voice="v", speed=None
        )
    assert "disagree on audio format" in str(ei.value)


async def test_merge_wav_concatenates_frames():
    """The merger itself: one WAV out, every input's frames, in order."""
    a, b = _wav(_pcm(7, 240)), _wav(_pcm(9, 120))
    merged = speak._merge_wavs([a, b])
    import io
    import wave

    with wave.open(io.BytesIO(merged), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000)
        raw = w.readframes(w.getnframes())
    assert raw == _pcm(7, 240) + _pcm(9, 120)


# ---- 4. the standard path is untouched -------------------------------------


async def test_within_cap_chunk_posts_verbatim_one_request(monkeypatch):
    """Servers with no practical cap (Kokoro, OpenAI at 4096) see exactly
    the #205 contract: one POST, input verbatim, raw bytes back."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(_pcm(3)))

    rec = _Recorder(handler)
    _install(monkeypatch, rec)

    audio = await speak.synthesize_remote(
        "Short one.",
        endpoint="http://herp.local:8081",
        api_key="sk-test",
        model="kokoro",
        voice="af_heart",
        speed=1.1,
    )
    assert audio == _wav(_pcm(3))
    assert len(rec.bodies) == 1
    assert rec.bodies[0] == {
        "model": "kokoro",
        "input": "Short one.",
        "voice": "af_heart",
        "response_format": "wav",
        "speed": 1.1,
    }
    assert speak._REMOTE_CAPS == {}
