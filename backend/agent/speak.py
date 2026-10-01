"""Text-to-speech for read-aloud of agent responses.

Engine: Kokoro-82M via sherpa-onnx (Apache-2.0 weights, ONNX Runtime,
CPU-only, no torch). Chosen over the reference `kokoro` pip package
because sherpa-onnx ships cp314 Windows wheels (the sidecar runs on
Python 3.14) and bundles its own ONNX Runtime instead of pulling torch.

Resolution mirrors transcribe.py: the packaged app drops the model under
<resource dir>/tts/kokoro..., dev runs use backend/data/tts, and
YAAH_TTS_MODEL_DIR overrides everything. The model is NOT bundled — the
user downloads it explicitly from Settings (POST /api/tts/download).

The engine is loaded lazily and kept as a module singleton; synthesis is
serialized behind a lock (sherpa's OfflineTts is not thread-safe) and
runs in worker threads via asyncio.to_thread at the endpoint layer.
"""
import os
import re
import shutil
import sys
import tarfile
import threading
import time
from pathlib import Path

import httpx

# ---- Voice table -----------------------------------------------------------
# Authoritative sid -> name order for the sherpa-onnx kokoro multi-lang v1.0
# voices.bin (scripts/kokoro/v1.0/generate_voices_bin.py in sherpa-onnx; the
# docs page predates em_santa and lists only 53). Prefixes: a=American English,
# b=British English; f=female, m=male. Other prefixes exist in the table but
# are non-English voices; the picker groups the English ones first.
VOICES: list[str] = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "ef_dora", "em_alex", "ff_siwis", "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "if_sara", "im_nicola", "jf_alpha", "jf_gongitsune", "jf_nezumi",
    "jf_tebukuro", "jm_kumo", "pf_dora", "pm_alex", "pm_santa",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi", "zm_yunjian",
    "zm_yunxi", "zm_yunxia", "zm_yunyang", "em_santa",
]

DEFAULT_VOICE = "af_heart"

# English voice ids (a*/b*); the picker shows these, grouped by accent.
ENGLISH_VOICES = [v for v in VOICES if v.startswith(("af_", "am_", "bf_", "bm_"))]

MODEL_NAME = "kokoro-int8-multi-lang-v1_0"
# Canonical packaged model (sherpa-onnx tts-models release). HF hosts the raw
# weights, but only this release ships the sherpa file set (voices.bin,
# tokens.txt, espeak-ng-data) as one archive.
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "tts-models/kokoro-int8-multi-lang-v1_0.tar.bz2"
)
MODEL_BYTES = 132_303_094  # shown in the Settings download button

_REQUIRED_FILES = ("model.int8.onnx", "voices.bin", "tokens.txt", "espeak-ng-data")


# ---- Model resolution ------------------------------------------------------

def _exe_dir() -> Path:
    """Directory of the running interpreter: backend.exe's folder when
    frozen (PyInstaller) or backend/ in dev."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent  # backend/


def _model_dir_candidates() -> list[Path]:
    """Where the kokoro model dir may live, best first. Tauri turns the
    `../` resource glob prefix into a literal `_up_` dir next to the exe
    (same layout quirk transcribe.py handles). YAAH_TTS_MODEL_DIR is
    authoritative when set — tests and custom builds point it at one dir
    and nothing else may shadow it. The download target (~/.yaah/tts or
    backend/data/tts) is always a candidate, so a freshly downloaded model
    resolves without a restart."""
    if os.environ.get("YAAH_TTS_MODEL_DIR"):
        return [Path(os.environ["YAAH_TTS_MODEL_DIR"])]
    exe = _exe_dir()
    out: list[Path] = []
    for base in (exe, exe / "_up_", Path.cwd()):
        out += [base / "tts" / MODEL_NAME, base / "backend" / "tts" / MODEL_NAME]
    out += [
        _data_root() / MODEL_NAME,  # where download_model() puts it
        Path(__file__).parent.parent / "data" / "tts" / MODEL_NAME,  # dev
        Path(__file__).parent.parent / "tts" / MODEL_NAME,  # repo checkout
    ]
    return out


def find_model_dir() -> Path | None:
    """First candidate dir containing the complete sherpa kokoro file set."""
    for d in _model_dir_candidates():
        if all((d / f).exists() for f in _REQUIRED_FILES):
            return d
    return None


def model_available() -> bool:
    return find_model_dir() is not None


def _data_root() -> Path:
    """Where downloads go: backend/data/tts in dev, ~/.yaah/tts frozen."""
    if getattr(sys, "frozen", False):
        root = Path.home() / ".yaah" / "tts"
    else:
        root = Path(__file__).parent.parent / "data" / "tts"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---- Download ---------------------------------------------------------------

_download_lock = threading.Lock()
_downloading = False


def download_in_progress() -> bool:
    return _downloading


def wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap 16-bit mono PCM in a minimal WAV container (in-memory; the
    browser's Web Audio API takes the blob directly)."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def download_model(progress) -> None:
    """Fetch and unpack the model archive, reporting progress as dicts:

    {"stage": "download", "received": n, "total": n}
    {"stage": "extract"}
    {"stage": "done"}
    {"stage": "error", "detail": str}

    Downloads to a .part file and extracts into a temp dir renamed into
    place, so a killed download never leaves a half model that resolves.
    """
    global _downloading
    with _download_lock:
        if _downloading:
            progress({"stage": "error", "detail": "download already running"})
            return
        _downloading = True
    try:
        _download_model_inner(progress)
    finally:
        with _download_lock:
            _downloading = False


def _download_model_inner(progress) -> None:
    import httpx

    target = _data_root() / MODEL_NAME
    if all((target / f).exists() for f in _REQUIRED_FILES):
        progress({"stage": "done"})
        return
    part = _data_root() / f"{MODEL_NAME}.tar.bz2.part"
    final = _data_root() / f"{MODEL_NAME}.tar.bz2"
    tmp_dir = _data_root() / f"{MODEL_NAME}.extracting"
    try:
        with httpx.stream("GET", MODEL_URL, timeout=60, follow_redirects=True) as res:
            res.raise_for_status()
            total = int(res.headers.get("content-length") or MODEL_BYTES)
            received = 0
            with open(part, "wb") as f:
                for chunk in res.iter_bytes(1024 * 256):
                    f.write(chunk)
                    received += len(chunk)
                    progress({"stage": "download", "received": received, "total": total})
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir()
        with tarfile.open(part, "r:bz2") as tar:
            tar.extractall(tmp_dir, filter="data")
        src = tmp_dir / MODEL_NAME
        if not all((src / f).exists() for f in _REQUIRED_FILES):
            raise RuntimeError("archive did not contain the expected model files")
        if target.exists():
            shutil.rmtree(target)
        src.replace(target)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        part.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        progress({"stage": "done"})
    except Exception as e:  # noqa: BLE001 — reported to the UI, not raised past the stream
        part.unlink(missing_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        progress({"stage": "error", "detail": str(e)[:300]})


# ---- Engine ------------------------------------------------------------------

_engine = None
_engine_lock = threading.Lock()
_synth_lock = threading.Lock()

# Replace semantics, server side. The frontend numbers utterances
# monotonically and sends each utterance's id with EVERY chunk request; the
# backend only ever raises its floor when the frontend pings /api/tts/stop
# with that utterance's id. superseded(e) == e <= floor, so:
#   - chunks of one utterance share an id -> concurrent prefetch never
#     aborts a live chunk (this killed all chat narration before: each
#     prefetch bumped a shared counter and aborted the chunk before it);
#   - starting utterance N pings stop(N): every older in-flight chunk
#     (id <= N) aborts at its next sentence boundary, N's own chunks live;
#   - stopping utterance N pings stop(N) too: its in-flight chunk aborts.
# Backend and frontend counters never need to agree on absolute values —
# only the frontend-provided ids are ever compared.
_floor = 0
_epoch_lock = threading.Lock()


class SupersededError(RuntimeError):
    """Raised when synthesis was aborted because a newer utterance replaced
    this one (floor raised by /api/tts/stop)."""


def ensure_epoch(floor: int) -> int:
    """Raise the supersede floor to `floor`; never lower it."""
    global _floor
    with _epoch_lock:
        if floor > _floor:
            _floor = floor
        return _floor


def superseded(epoch: int) -> bool:
    with _epoch_lock:
        return epoch <= _floor


def _import_engine():
    """Import sherpa_onnx, preloading its bundled DLLs on Windows first.

    The wheel's .pyd links onnxruntime.dll by bare name; the standard DLL
    search order checks System32 before the package's lib dir, so a broken
    same-named DLL there poisons the import (observed on a dev box: a
    2.8 KB bogus onnxruntime.dll in System32 -> '%1 is not a valid Win32
    application'). Preloading by absolute path wins the race and also
    covers the packaged app, where sherpa's lib dir sits next to the exe.
    """
    try:
        import sherpa_onnx

        return sherpa_onnx
    except ImportError:
        if os.name != "nt":
            raise
    import ctypes
    import importlib.util

    spec = importlib.util.find_spec("sherpa_onnx")
    if spec is None or spec.submodule_search_locations is None:
        raise
    base = Path(list(spec.submodule_search_locations)[0]) / "lib"
    for name in ("onnxruntime.dll", "sherpa-onnx-c-api.dll", "sherpa-onnx-cxx-api.dll"):
        p = base / name
        if p.exists():
            ctypes.WinDLL(str(p))
    import sherpa_onnx

    return sherpa_onnx


def get_engine():
    """Lazy singleton OfflineTts; None when the model is not downloaded."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        model_dir = find_model_dir()
        if model_dir is None:
            return None
        sherpa = _import_engine()
        model_file = "model.int8.onnx"
        if not (model_dir / model_file).exists():
            model_file = "model.onnx"
        lexicon = ",".join(
            str(model_dir / f)
            for f in ("lexicon-us-en.txt", "lexicon-gb-en.txt")
            if (model_dir / f).exists()
        )
        cfg = sherpa.OfflineTtsConfig(
            model=sherpa.OfflineTtsModelConfig(
                num_threads=4,
                kokoro=sherpa.OfflineTtsKokoroModelConfig(
                    model=str(model_dir / model_file),
                    voices=str(model_dir / "voices.bin"),
                    tokens=str(model_dir / "tokens.txt"),
                    data_dir=str(model_dir / "espeak-ng-data"),
                    lexicon=lexicon,
                ),
            ),
            max_num_sentences=1,  # Kokoro ignores other values (auto-batch is VITS-only)
        )
        t0 = time.time()
        tts = sherpa.OfflineTts(cfg)
        # First generate warms the graph; without it the first user-facing
        # sentence pays a multi-second JIT-style tax.
        tts.generate(text="Ready.", sid=0, speed=1.0)
        _engine = tts
        print(f"[tts] kokoro ready in {time.time() - t0:.1f}s ({model_dir})", flush=True)
        return _engine


def voice_id(name: str) -> int:
    """sid for a voice name; unknown names fall back to the default."""
    try:
        return VOICES.index(name)
    except ValueError:
        return VOICES.index(DEFAULT_VOICE)


def synthesize(text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0, epoch: int | None = None):
    """Synthesize one chunk; returns (pcm16le bytes, sample_rate).

    epoch: the utterance's generation. When the supersede floor has been
    raised past it (a replacement utterance pinged /api/tts/stop), synthesis
    aborts at the next internal sentence boundary and raises SupersededError
    — the caller drops the stale chunk. Chunks of the same utterance share
    one epoch, so concurrent prefetch never aborts a live chunk.

    Raises RuntimeError when the model is missing; the caller (endpoint)
    turns that into a 409 so the UI can offer the download.
    """
    tts = get_engine()
    if tts is None:
        raise RuntimeError("TTS model not downloaded")
    speed = max(0.5, min(2.0, float(speed) or 1.0))
    if epoch is not None and superseded(epoch):
        raise SupersededError("superseded before synthesis")
    with _synth_lock:
        if epoch is not None and superseded(epoch):
            raise SupersededError("superseded under lock")

        def check_stale(_samples, _progress) -> int:
            # sherpa's convention (verified by probe, cb_semantics): return 1
            # = CONTINUE, return 0 = STOP — the opposite of what you'd guess.
            # It fires between internal sentences; 0 discards the rest.
            return 0 if epoch is not None and superseded(epoch) else 1

        audio = tts.generate(text=text, sid=voice_id(voice), speed=speed, callback=check_stale)
    from array import array

    pcm = array("h", (int(max(-1.0, min(1.0, s)) * 32767) for s in audio.samples))
    if epoch is not None and superseded(epoch):
        raise SupersededError("superseded after synthesis")
    return pcm.tobytes(), audio.sample_rate


# ---- Remote engine (OpenAI-compatible /v1/audio/speech, #205) ----------------
# A STANDARD OpenAI speech client: POST {base}/v1/audio/speech with
# model/input/voice (+ speed only when the user set one) and
# response_format=wav; raw audio bytes come back on 200. The owner-run
# Kokoro server (herp.local) is a faithful compatible subset of this
# contract, and api.openai.com works with tts-1 / gpt-4o-mini-tts — never
# send the newer surface (instructions, stream_format/sse, custom voices).

REMOTE_TIMEOUT = 10.0  # seconds; a LAN GPU box answers well inside this
REMOTE_ATTEMPTS = 2  # one retry, then the chunk is skipped (the frontend
# degrades to the visible text — narration must never stall the chat)


class RemoteTTSError(RuntimeError):
    """A remote synthesis request failed. The message is user-facing:
    the synthesize endpoint and Settings' Test button surface it."""

    def __init__(self, message: str, code: str = "remote-failed"):
        super().__init__(message)
        # Machine-readable cause for UI branching: "not-configured" (no
        # endpoint set), "remote-failed" (server refused / unreachable).
        self.code = code


def _remote_url(base: str) -> str:
    """/v1/audio/speech appended exactly once — accept a bare host:port
    base, one ending in /v1, or the full suffix path."""
    trimmed = (base or "").strip().rstrip("/")
    if not trimmed:
        raise RemoteTTSError(
            "remote TTS selected but no endpoint configured (Settings → Voice)",
            code="not-configured",
        )
    if trimmed.endswith("/v1/audio/speech"):
        return trimmed
    if trimmed.endswith("/v1"):
        return trimmed + "/audio/speech"
    return trimmed + "/v1/audio/speech"


def _remote_error_detail(res: httpx.Response) -> str:
    """Pull the server's message out of an error response, tolerantly:
    OpenAI answers {"error": {"message": ...}}, FastAPI-based servers
    (herp.local) answer {"detail": ...}, anything else is shown raw."""
    status = f"HTTP {res.status_code}"
    try:
        body = res.json()
    except ValueError:
        text = (res.text or "").strip()
        return f"{status}: {text[:200]}" if text else status
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return f"{status}: {err['message']}"
        if isinstance(err, str) and err:
            return f"{status}: {err}"
        if body.get("detail"):
            return f"{status}: {body['detail']}"
    return f"{status}: {res.text[:200]}".rstrip()


async def synthesize_remote(
    text: str,
    endpoint: str,
    api_key: str,
    model: str,
    voice: str,
    speed: float | None,
) -> bytes:
    """One chunk through the remote engine; returns the server's WAV bytes
    (the endpoint pipes them to the frontend untouched, Web Audio decodes).

    Error posture (the spec's contract): a 4xx is the server's final
    answer — one request, no retry. Timeouts and 5xx get one retry, then
    RemoteTTSError carries the server's message. An empty 200 body is a
    broken server, not silence. The endpoint maps failures to 502; the
    frontend skips the chunk and keeps the text visible.
    """
    url = _remote_url(endpoint)
    body: dict = {"model": model, "input": text, "voice": voice, "response_format": "wav"}
    if speed is not None:
        body["speed"] = speed
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    last_err: str | None = None
    for _ in range(REMOTE_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=REMOTE_TIMEOUT) as client:
                res = await client.post(url, json=body, headers=headers)
            if res.status_code == 200:
                audio = res.content
                if not audio:
                    raise RemoteTTSError("remote TTS returned an empty response")
                return audio
            last_err = _remote_error_detail(res)
            if res.status_code < 500:
                break  # 4xx: retrying the same request cannot help
        except RemoteTTSError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = f"connection failed: {e}"
    raise RemoteTTSError(last_err or "remote TTS failed")


# ---- Voice discovery (#231): best-effort, non-standard GET /voices --------


VOICES_TIMEOUT = 4.0  # discovery is best-effort; a slow answer is a no-list


def _voices_url(base: str) -> str:
    """/voices on the server ROOT — verified on the reference Kokoro server,
    where /v1/voices is 404 even though speech lives under /v1. Accepts the
    same bases as _remote_url (bare host:port, a /v1-suffixed base) plus a
    user who typed the voices path itself."""
    trimmed = (base or "").strip().rstrip("/")
    if not trimmed:
        raise RemoteTTSError(
            "remote TTS selected but no endpoint configured (Settings → Voice)",
            code="not-configured",
        )
    for suffix in ("/voices", "/v1/audio/voices", "/v1/voices"):
        if trimmed.endswith(suffix):
            return trimmed[: len(trimmed) - len(suffix)] + "/voices"
    if trimmed.endswith("/v1"):
        return trimmed[:-3] + "/voices"
    return trimmed + "/voices"


def _normalize_voices(body) -> list[str]:
    """Best-effort extraction of voice names from whatever shape a server
    answers with: a bare array (the reference server), {"voices": [...]},
    {"data": [{"id": ...}]} (OpenAI list style), or a name-keyed map of
    descriptors. Drops junk, collapses duplicates, preserves server order.
    A dict counts as a name-keyed map only when every value is a
    descriptor (dict/list) — {"ok": true} is not a voice list."""
    if isinstance(body, list):
        candidates: list = [
            v.get("id") if isinstance(v, dict) else v for v in body
        ]
    elif isinstance(body, dict):
        if isinstance(body.get("voices"), list):
            candidates = list(body["voices"])
        elif isinstance(body.get("data"), list):
            candidates = [
                v.get("id") if isinstance(v, dict) else v for v in body["data"]
            ]
        elif body and all(isinstance(v, (dict, list)) for v in body.values()):
            candidates = list(body.keys())
        else:
            return []
    else:
        return []
    names = [c.strip() for c in candidates if isinstance(c, str) and c.strip()]
    return list(dict.fromkeys(names))


async def probe_remote_voices(endpoint: str, api_key: str = "") -> list[str]:
    """The voice list a server offers, or [] when it offers none. ANY
    failure — 404, 5xx, timeout, unreachable, unparseable body — reads as
    "no list": OpenAI's own API has no voices endpoint, so a list-less
    server is normal, not broken (#205: discovery is a nicety, never a
    requirement). Only a missing endpoint raises (not-configured, the
    same gate as speech)."""
    url = _voices_url(endpoint)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=VOICES_TIMEOUT) as client:
            res = await client.get(url, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError):
        return []
    if res.status_code != 200:
        return []
    try:
        body = res.json()
    except ValueError:
        return []
    return _normalize_voices(body)


# ---- Spoken briefing (two-channel split, #66) ---------------------------------
# The chat transcript and the TTS input are deliberately DIFFERENT texts. The
# agent emits a condensed spoken line as a <say> tag at the end of its final
# answer (system prompt asks for it); the backend strips the tag from the
# transcript and ships the line separately as a `say` stream event. When the
# tag is missing or useless, a local heuristic derives a briefing from the
# prose itself — the fallback is today's truncated verbatim read, never
# silence.

# Accept harmless whitespace around the tag name/delimiters: the model can
# emit malformed variants like '< say>' or '</say >'. Only closed tags count
# as a spoken briefing; transcript stripping is more lenient below.
SAY_TAG = re.compile(r"<\s*say\s*>(.*?)</\s*say\s*>\s*$", re.S | re.I)
SAY_TAG_ANY = re.compile(r"<\s*say\s*>(.*?)</\s*say\s*>", re.S | re.I)
# A truncated trailing briefing is still not chat, even without a closing
# tag — but ONLY while it is briefing-sized (SAY_MAX_CHARS). An unclosed
# opener followed by a longer body is a stray tag inside a real answer:
# blanking the message from the opener onward blanked whole transcripts
# (be053e5 regression), so the swallow is now length-gated.
SAY_TAG_UNCLOSED = re.compile(r"<\s*say\s*>([\s\S]*)$", re.I)
SAY_TAG_PARTIAL = re.compile(r"<\s*(?:s(?:a(?:y)?)?)?\s*$", re.I)
SAY_TAG_CLOSE = re.compile(r"</\s*say\s*>", re.I)

# Hard cap for the spoken line, in ONE place per side (mirrored in speech.ts).
# ~400 chars ≈ 20–30 s of audio, far under the old 4000-char verbatim cap.
SAY_MAX_CHARS = 400

# Char budget the heuristic (and the cap on a model-emitted line) aims for.
_BRIEFING_MAX = SAY_MAX_CHARS


def _strip_say_tags(content: str) -> str:
    """Remove complete tags anywhere and a trailing truncated briefing."""
    text = SAY_TAG_CLOSE.sub("", SAY_TAG_ANY.sub("", content))
    m = SAY_TAG_UNCLOSED.search(text)
    # Only swallow the unclosed tail when it is briefing-sized; a longer body
    # after an unclosed opener is real chat and must survive (see regex note).
    if m and len(m.group(1)) <= SAY_MAX_CHARS:
        text = text[: m.start()]
    return SAY_TAG_PARTIAL.sub("", text).rstrip()


def extract_say(content: str) -> tuple[str, str | None]:
    """Split a final assistant message into (chat text, spoken line).

    The last nonempty, closed briefing is returned as speech. Complete tags
    and a trailing unterminated briefing are omitted from the transcript.
    """
    said: str | None = None
    for m in SAY_TAG_ANY.finditer(content):
        if m.group(1).strip():
            said = m.group(1).strip()
    return _strip_say_tags(content), said


def strip_say_tags(content: str) -> str:
    """Chat-transcript view of a message: <say> tags removed."""
    return _strip_say_tags(content)


def heuristic_briefing(md: str, max_chars: int = _BRIEFING_MAX) -> str:
    """Fallback briefing without a model call: the first paragraph's first
    sentence plus the final sentence of the prose, whichever fits the
    budget. Semantic but cheap — 'what did this turn conclude' approximated
    by 'how did it open and close'."""
    prose = prose_for_speech(md, max_chars=10**9)  # formatting only, no cap
    if not prose:
        return ""
    paras = [p.strip() for p in prose.split("\n") if p.strip()]
    if not paras:
        return ""
    first = paras[0]
    m = _SENT_END_ANY.search(first)
    opening = first[: m.end(1)].strip() if m else first
    tail = paras[-1]
    m = None
    for m in _SENT_END_ANY.finditer(tail):
        pass
    closing = tail[: m.end(1)].strip() if m else tail
    if closing and closing != opening and len(opening) + len(closing) + 1 <= max_chars:
        return f"{opening} {closing}".strip()
    # One of the two alone, clipped at a sentence boundary under the cap.
    pick = opening if len(opening) >= len(closing) else closing
    if len(pick) <= max_chars:
        return pick
    cut = pick[:max_chars]
    m2 = None
    for m2 in _SENT_END.finditer(cut):
        pass
    if m2 and m2.end() > max_chars // 2:
        cut = cut[: m2.end()]
    return cut.strip()


def spoken_line(briefing: str | None, md: str, max_chars: int = _BRIEFING_MAX) -> str:
    """The final TTS input for a turn: the model-emitted briefing when
    usable, else the heuristic, else the old truncated verbatim prose (the
    fallback is today's behavior, never silence). Everything passes through
    prose_for_speech so no markdown junk reaches the synthesizer, and the
    hard cap is enforced here — in one place."""
    def _finish(text: str) -> str:
        # #206: the speech-normalization pass is the last thing a briefing
        # receives before the synthesizer. The cap is re-enforced after it:
        # spelled-out numbers ("four four three") are longer than digits.
        out = normalize_for_speech(text)
        return _clip(out, max_chars) if len(out) > max_chars else out

    prose = prose_for_speech(md, max_chars=10**9)
    source = (briefing or "").strip()
    if source:
        line = prose_for_speech(source, max_chars=10**9)
        if len(line) <= max_chars:
            return _finish(line)
        return _finish(heuristic_briefing(line, max_chars) or _clip(line, max_chars))
    return _finish(heuristic_briefing(prose, max_chars) or _clip(prose, max_chars))


def _clip(text: str, max_chars: int) -> str:
    """Last-resort sentence-boundary truncation (the old 4000-cap behavior,
    at the briefing budget)."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    m = None
    for m in _SENT_END.finditer(cut):
        pass
    if m and m.end() > max_chars // 2:
        cut = cut[: m.end()]
    return cut.strip()


# ---- Text preparation --------------------------------------------------------

_ABBREV = {
    "e.g", "i.e", "etc", "vs", "cf", "ca", "Dr", "Mr", "Mrs", "Ms", "Prof",
    "St", "Sr", "Jr", "Fig", "No", "Vol", "Ch", "Sec", "approx", "resp",
    "min", "max", "resp", "Inc", "Ltd", "Co", "Corp",
}
_SENT_END = re.compile(r"([.!?]+[\"')\]]?)\s+")
_WS = re.compile(r"[ \t]+")


def split_sentences(text: str, min_len: int = 80, max_len: int = 300) -> list[str]:
    """Split prose into synthesis chunks: sentences merged up to min_len so
    Kokoro gets enough context for natural prosody, hard-split at max_len
    so first audio arrives fast. Abbreviations don't end a sentence."""
    text = _WS.sub(" ", text.strip())
    if not text:
        return []
    raw: list[str] = []
    start = 0
    for m in _SENT_END.finditer(text):
        end = m.end()
        # The token after the boundary decides: an abbreviation ("e.g.",
        # "Dr") or a lowercase continuation keeps the sentence open.
        tail = text[m.end():m.end() + 6]
        word = re.match(r"[A-Za-z]+", tail)
        before = text[max(0, m.start() - 6):m.start() + 1]
        last_word = re.search(r"([A-Za-z.]+)$", before)
        if (
            word
            and not word.group(0)[0].isupper()
            and word.group(0).lower() not in ("i",)
        ):
            continue
        if last_word and last_word.group(1).rstrip(".") in _ABBREV:
            continue
        raw.append(text[start:end])
        start = end
    if start < len(text):
        raw.append(text[start:])

    # Merge short sentences; hard-split monsters at a word boundary.
    out: list[str] = []
    buf = ""
    for s in raw:
        if buf and len(buf) + len(s) > max_len:
            out.append(buf.strip())
            buf = ""
        if len(s) > max_len:
            while len(s) > max_len:
                cut = s.rfind(" ", 0, max_len)
                cut = cut if cut > max_len // 2 else max_len
                out.append(s[:cut].strip())
                s = s[cut:]
            buf = s
        else:
            buf += s
        if len(buf) >= min_len:
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return [s for s in out if s]


_SENT_END_ANY = re.compile(r"([.!?]+[\"')\]]?)(\s+|$)")
_FENCE = re.compile(r"```.*?```", re.S)

_INDENT_BLOCK = re.compile(r"(?m)^(?:    |\t).*(?:\n|$)+")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_TABLE_ROW = re.compile(r"(?m)^\s*\|.*\|\s*$")
_HEADING = re.compile(r"(?m)^#{1,6}\s+")
_LIST_MARK = re.compile(r"(?m)^\s*[-*+]\s+")
# Emphasis: pair delimiters only when not glued to word chars, so code
# identifiers (tts_enabled, max_tokens) keep their underscores.
_BOLD = re.compile(
    r"\*\*([^*]+)\*\*|\*([^*]+)\*|__([^_]+)__|(?<![A-Za-z0-9_])_([^_]+)_(?![A-Za-z0-9_])"
)
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")

# Emoji: espeak-ng (Kokoro's text front-end) looks emoji up in its dictionary
# and SPEAKS THEIR NAMES ("waving hand sign", ~0.8-1.7s each, probe-verified).
# Emoji carry no meaning read aloud, so they are stripped before synthesis.
# Ranges: all supplementary emoji blocks, misc symbols + dingbats (✅⚠❌),
# misc-symbols-and-arrows block (⭐), regional indicators, variation
# selectors, ZWJ and keycap (emoji ZWJ sequences collapse to nothing).
# Deliberately NOT stripped: →/← (U+2190-21FF) — legitimate technical prose.
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D\U000020E3"
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text: str) -> str:
    """Remove emoji and repair the spacing they leave behind."""
    t = _EMOJI.sub("", text)
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)  # "time 🙂." -> "time."
    t = re.sub(r"  +", " ", t)  # "end 😄 just" -> "end just"
    return t.strip()


def prose_for_speech(md: str, max_chars: int = 4000) -> str:
    """Reduce an assistant markdown message to speakable prose: fenced and
    indented code blocks become pauses (dropped), tables drop, links keep
    their label, emphasis markers strip. Long reads truncate at a sentence
    boundary — the full text stays on screen. (The verbatim read's 4000-char
    cap is now only the last-resort fallback path; the spoken line normally
    comes from spoken_line() at the briefing budget.)"""
    if not md:
        return ""
    t = _FENCE.sub("\n\n", md)
    t = _INDENT_BLOCK.sub("\n\n", t)
    t = _TABLE_ROW.sub("", t)
    t = _IMAGE.sub("", t)
    t = _LINK.sub(r"\1", t)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _HEADING.sub("", t)
    t = _LIST_MARK.sub("", t)
    t = _BOLD.sub(lambda m: m.group(1) or m.group(2) or m.group(3) or m.group(4) or "", t)
    t = _TAG.sub("", t)
    t = strip_emoji(t)
    t = _SPACES.sub(" ", t)
    t = _BLANKS.sub("\n\n", t).strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    # Back off to the last sentence end so the truncation isn't mid-word.
    m = None
    for m in _SENT_END.finditer(cut):
        pass
    if m and m.end() > max_chars // 2:
        cut = cut[: m.end()]
    return cut.strip()


# ---- Speech normalization (#206) -----------------------------------------------
# Kokoro reads for the ear; the agent writes for the eye. This pass turns
# written forms into speakable forms deterministically (no model call): digit
# runs by context, years in year-form, dotted version/IP groups, phone groups,
# emotion markers and SSML stripped, curated acronyms spelled out. Mirrored in
# src/speech.ts (normalizeForSpeech) — keep the shared test cases identical
# on both sides (backend/tests/test_speak.py SHARED_CASES).

_DIGITS = {"0": "oh", "1": "one", "2": "two", "3": "three", "4": "four",
           "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}
_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
         "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]

# A number token NOT glued to letters and not part of a longer decimal:
# a trailing sentence period or comma is fine, "1999.5" / "2024s" are not
# this token.
_NOT_FRAG = r"(?!\.?\d)(?![A-Za-z])"

# Versionish token: a dotted numeric group run (version or IP-like), with an
# optional v/version head and an end-attached suffix (-rc.7, +build21).
_VERSIONISH = re.compile(
    r"(?<![\w.])(v(?:er(?:sion)?)?\s?)?(\d{1,3}(?:\.\d{1,3})+)"
    r"((?:[-+][A-Za-z][\w]*(?:\.[\w]+)*)?)"
)

# Emotion markers / stage directions: [excited], [sighs], [pause]. Only
# short all-word contents count, so bracketed technical prose — the
# documented IPA escape hatch ([dʒeɪson]) — never matches.
_EMOTION_WORDS = (
    "excited", "sad", "happy", "angry", "sarcastic", "whispers", "whisper",
    "shouts", "shouting", "laughs", "laughing", "giggles", "sighs", "sigh",
    "pause", "pauses", "breath", "excitedly", "calmly", "seriously",
    "dramatically", "nervously", "cheerful", "cheerfully", "tone", "ironic",
)
_EMOTION_MARKER = re.compile(r"\[([^\[\]]+)\]")

# SSML fragments are stripped, never read as phonemes.
_SSML_TAG = re.compile(
    r"</?\s*(?:speak|break|prosody|say-?as|phoneme|sub|emphasis|voice|"
    r"p|s|amazon:[\w-]+|google:[\w-]+)(?:\s[^>]*)?/?>",
    re.I,
)


def _is_emotion_marker(inner: str) -> bool:
    words = inner.split()
    return 1 <= len(words) <= 3 and all(
        w.lower().strip("'’-") in _EMOTION_WORDS for w in words
    )


def _under_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] + ("-" + _ONES[o] if o else "")


def _two_digit(n: int) -> str:
    """0-99 the way digits are said inside grouped runs."""
    return _DIGITS[str(n)] if n < 10 else _under_hundred(n)


def _group_words(group: str) -> str:
    """One dotted-group segment the way a human reads it: single digits as
    digit words (the 'oh' of one-point-oh), 10-99 natural (sixteen), and
    three-digit groups digit-by-digit (bytes in an address)."""
    n = int(group)
    if len(group) == 1 or (len(group) == 2 and n < 10):
        return _DIGITS[group.lstrip("0") or "0"]
    if len(group) == 2:
        return _under_hundred(n)
    return " ".join(_DIGITS[d] for d in group)


def _year_words(d: str) -> str:
    """A four-digit year the way a human says it (1999, 2016, 1800)."""
    hi, lo = int(d[:2]), int(d[2:])
    if hi < 10:
        return _under_hundred(int(d))
    if hi == 10:
        return "ten hundred"
    hundreds = _under_hundred(hi)
    if lo == 0:
        return f"{hundreds} hundred"
    if lo < 10:
        return f"{hundreds} oh {_DIGITS[str(lo)]}"
    return f"{hundreds} {_two_digit(lo)}"


def _digit_run_words(run: str) -> str:
    return " ".join(_DIGITS[d] for d in run)


def _phone_words(token: str) -> str:
    """Phone grouping with a spoken pause at group edges. Hyphenated tokens
    split at their hyphens (555-0100, 1-800-555-0199); bare 10-digit runs
    group North-American style 3+3+rest."""
    if "-" in token:
        groups = [g for g in token.split("-") if g]
        if groups[0] == "1" and len(groups) > 3:
            groups = groups[1:]  # country code absorbed silently
    else:
        digits = re.sub(r"\D", "", token)
        if len(digits) >= 10:
            groups = [digits[:3], digits[3:6], digits[6:]]
        else:
            groups = [digits]
    return ", ".join(_digit_run_words(g) for g in groups)


def _is_phone(token: str) -> bool:
    digits = re.sub(r"\D", "", token)
    if "-" in token:
        return 7 <= len(digits) <= 11
    return len(digits) in (10, 11)


def _version_words(m: "re.Match") -> str:
    head, dotted, suffix = m.group(1) or "", m.group(2), m.group(3) or ""
    groups = dotted.split(".")
    # A bare two-group decimal with a single-digit head (3.14, 1.3, 0.5) is
    # an ordinary number: the engines already read it correctly. Versions
    # carry a head; IPs carry four groups.
    if not head and len(groups) == 2 and len(groups[0]) == 1:
        return m.group(0)
    joiner = " dot " if len(groups) >= 4 else " point "
    out = "version " if head else ""
    out += joiner.join(_group_words(g) for g in groups)
    if suffix:
        bits: list[str] = []
        for p in re.findall(r"\d+|[A-Za-z]+|\.", suffix):
            if p == ".":
                bits.append("point")
            elif p[0].isdigit():
                bits.append(_two_digit(int(p)) if len(p) <= 2
                            else _digit_run_words(p))
            elif len(p) == 1 or p.lower() == "rc":
                bits.append(" ".join(c.upper() for c in p))
            else:
                bits.append(p)  # beta, dev, rel — real words, keep them
        out += " " + " ".join(bits)
    return out


def _acronyms(text: str) -> str:
    """Curated acronyms to letter-spell; unlisted words (Kokoro, ONNX) are
    left to the engine's lexicon."""
    known = {"TLS", "SSL", "HTTP", "HTTPS", "API", "CPU", "GPU", "RAM",
             "URL", "URI", "SQL", "JSON", "XML", "HTML", "CSS", "WAV",
             "PDF", "PNG", "JWT", "CLI", "SDK", "LLM", "WSL", "UI",
             "UUID", "SIP", "SSH", "TCP", "UDP", "DNS", "IP", "CI", "CD"}
    return " ".join(
        " ".join(w) if w in known else w for w in text.split(" ")
    )


def normalize_for_speech(text: str) -> str:
    """Deterministic written→spoken pass over TTS INPUT only (the chat
    transcript is untouched). Order matters: markers/SSML first so their
    digits never feed the number rules, then years before bare runs, then
    versionish dotted groups, then long digit runs by context (phones
    group, everything else digit-by-digit), then short numbers, then
    acronyms."""
    if not text:
        return text

    # 1. SSML tags and emotion markers cannot survive into synthesis.
    t = _SSML_TAG.sub(" ", text)
    t = _EMOTION_MARKER.sub(
        lambda m: "" if _is_emotion_marker(m.group(1)) else m.group(0), t,
    )
    t = re.sub(r"  +", " ", t)
    t = re.sub(r" ([,.!?;:])", r"\1", t)

    # 2. Years (1000-2999) before generic digit-run rules.
    t = re.sub(r"(?<![\w.])([12]\d{3})" + _NOT_FRAG,
               lambda m: _year_words(m.group(1)), t)

    # 3. Versionish dotted groups (v1.0.16-rc.7, 10.0.0.1).
    t = _VERSIONISH.sub(_version_words, t)

    # 4. Long digit runs (and phones with hyphens): phones group, all else
    #    reads digit-by-digit (ports, IDs).
    def _run(m: "re.Match") -> str:
        token = m.group(1)
        if _is_phone(token):
            return _phone_words(token)
        return _digit_run_words(re.sub(r"\D", "", token))

    t = re.sub(r"(?<![\w.])(\d[\d-]{3,})" + _NOT_FRAG, _run, t)

    # 5. Remaining short numbers: two digits natural (sixteen), three
    #    digits digit-by-digit (443 is a port far more often than a
    #    quantity in this domain); single digits stay.
    def _short(m: "re.Match") -> str:
        n = m.group(1)
        return _two_digit(int(n)) if len(n) == 2 else _digit_run_words(n)

    t = re.sub(r"(?<![\w.])(\d{2,3})" + _NOT_FRAG, _short, t)

    # 6. Acronyms letter-spelled.
    return _acronyms(t).strip()


def normalize_prose_for_speech(md: str, max_chars: int = 4000) -> str:
    """The composition TTS input receives: markdown → prose, then the
    #206 normalization pass."""
    return normalize_for_speech(prose_for_speech(md, max_chars=max_chars))
