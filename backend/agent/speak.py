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
import io
import os
import re
import shutil
import sys
import tarfile
import threading
import time
import wave
from pathlib import Path

import httpx

from backend.agent import gitproc
from backend.agent.config import load_config

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

# Canonical local model: kokoro fp32 (#298). The int8 pack emits steady
# iSTFT-frame tones at sr/5 and 2*sr/5 (+22..34 dB over the noise floor,
# still ringing in sentence pauses); fp32 measures clean, synthesizes ~3x
# faster than real time on CPU (int8 was slower than real time), and its
# tarball costs ~350 MB once. YAAH_TTS_MODEL_DIR at a pinned int8 pack
# keeps working (advanced escape hatch) — see _MODEL_FILES.
MODEL_NAME = "kokoro-multi-lang-v1_0"
# Canonical packaged model (sherpa-onnx tts-models release). HF hosts the raw
# weights, but only this release ships the sherpa file set (voices.bin,
# tokens.txt, espeak-ng-data) as one archive.
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "tts-models/kokoro-multi-lang-v1_0.tar.bz2"
)
MODEL_BYTES = 349_906_910  # shown in the Settings download button

# Support files every kokoro pack carries, plus the weights file(s) a pack
# may provide — a dir resolves only with the support set AND at least one
# weights file; the FIRST existing entry of _MODEL_FILES is the one the
# engine loads (fp32 preferred; int8 packs still resolve for pinned dirs).
_SUPPORT_FILES = ("voices.bin", "tokens.txt", "espeak-ng-data")
_MODEL_FILES = ("model.onnx", "model.int8.onnx")

# Production synthesis threads; the harness (scripts/measure_tts_whine.py)
# reports against this baseline, so it lives here and not as a literal.
_NUM_THREADS = 4


def _pack_complete(model_dir: Path) -> bool:
    """True when model_dir carries the sherpa support set plus a weights
    file (the #298 resolution rule, single-sourced)."""
    return all((model_dir / f).exists() for f in _SUPPORT_FILES) and any(
        (model_dir / f).exists() for f in _MODEL_FILES
    )


# ---- Model resolution ------------------------------------------------------


def _model_dir_candidates() -> list[Path]:
    """Where the kokoro model dir may live, best first. See
    gitproc.bundle_dirs for the packaged `_up_` layout. YAAH_TTS_MODEL_DIR
    is authoritative when set — tests and custom builds point it at one
    dir and nothing else may shadow it. The download target (~/.yaah/tts
    or backend/data/tts) is always a candidate, so a freshly downloaded
    model resolves without a restart."""
    if os.environ.get("YAAH_TTS_MODEL_DIR"):
        return [Path(os.environ["YAAH_TTS_MODEL_DIR"])]
    out: list[Path] = gitproc.bundle_dirs("tts", MODEL_NAME)
    out += [
        _data_root() / MODEL_NAME,  # where download_model() puts it
        Path(__file__).parent.parent / "data" / "tts" / MODEL_NAME,  # dev
        Path(__file__).parent.parent / "tts" / MODEL_NAME,  # repo checkout
    ]
    return out


def _model_file_for(model_dir: Path) -> str:
    """The weights file a resolved pack carries (fp32 preferred over int8 —
    see _MODEL_FILES; pinned int8 dirs keep working via YAAH_TTS_MODEL_DIR).
    Callers must have resolved model_dir through find_model_dir() — an
    incomplete dir raises StopIteration here, by design."""
    return next(f for f in _MODEL_FILES if (model_dir / f).exists())


def find_model_dir() -> Path | None:
    """First candidate dir with the sherpa kokoro support set AND a loadable
    weights file (fp32 preferred over int8 — see _MODEL_FILES)."""
    for d in _model_dir_candidates():
        if _pack_complete(d):
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
    target = _data_root() / MODEL_NAME
    if _pack_complete(target):
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
        if not _pack_complete(src):
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
        model_file = _model_file_for(model_dir)
        lexicon = ",".join(
            str(model_dir / f)
            for f in ("lexicon-us-en.txt", "lexicon-gb-en.txt")
            if (model_dir / f).exists()
        )
        cfg = sherpa.OfflineTtsConfig(
            model=sherpa.OfflineTtsModelConfig(
                num_threads=_NUM_THREADS,
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

# Per-request ceiling, not a wait (#448): a GPU box answers in seconds, but
# the owner's MOSS-TTS-Realtime box serves on CPU at ~6x realtime and measured
# 63 s cold / ~118 s warm for one short sentence — the old 10 s (tuned for a
# GPU LAN box) gave up mid-synthesis and read as "connection failed". 180 s
# covers the observed CPU profile with slack; a timeout still surfaces fast
# RELATIVE to the work, and narration stays async.
REMOTE_TIMEOUT = 180.0
# Connection establishment keeps a short leash (review, #449): the
# budget above pays for the server's SYNTHESIS work (read phase). A
# dead host (powered off, firewall silently dropping packets) must
# fail in seconds as "connection failed", not burn the full budget
# and then be mis-reported as "still synthesizing".
REMOTE_CONNECT_TIMEOUT = 5.0
REMOTE_ATTEMPTS = 2  # one retry, then the chunk is skipped (the frontend
# degrades to the visible text — narration must never stall the chat)


class RemoteTTSError(RuntimeError):
    """A remote synthesis request failed. The message is user-facing:
    the synthesize endpoint and Settings' Test button surface it. timed_out
    distinguishes "server took too long" (#448: normal for CPU-served
    models) from "unreachable/refused" — recovery advice differs."""

    def __init__(self, message: str, code: str = "remote-failed"):
        super().__init__(message)
        self.timed_out = False
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


# ---- Adaptive input splitting (servers that cap `input`) ---------------------
# The speech contract caps `input` wherever each server pleases, and each
# server says so only in its own rejection: OpenAI documents 4096 chars;
# the MOSS-TTS-Realtime wrapper rejects anything over 48 (its model tops
# out at ~41 s of audio per request). /voices carries no hint, so the
# client LEARNS the cap from a "too long" HTTP 4xx: one sentence-bounded
# re-split at the stated cap (any "too long" 4xx counts; halving the
# failed input when the rejection states no number), pieces synthesized in order and merged into a single WAV (same
# server/voice => identical format; stdlib wave). The learned cap is
# cached per origin, so only the first-ever oversized chunk pays the
# rejected-request tax. Any OTHER 4xx (bad voice, bad model) is not a
# length problem: it surfaces verbatim, never split, never retried.

_REMOTE_CAPS: dict[str, int] = {}  # origin -> learned max `input` chars

_SPLITTABLE_ERR = re.compile(r"^HTTP 4\d\d\b")  # a 4xx detail, not a timeout
_TOO_LONG_RE = re.compile(
    r"\btoo\s+long\b"
    r"|(?:chars?|characters?)\s*max"
    r"|max(?:imum)?\s*(?:of\s+)?\d+\s*(?:chars?|characters?)"
    r"|(?:at\s+most|up\s+to)\s+\d+\s*(?:chars?|characters?)"
    r"|(?:chars?|characters?)\s+long\b",
    re.IGNORECASE,
)
_CAP_RE = re.compile(
    r"(?P<a>\d{1,5})\s*(?:chars?|characters?)\s*max(?:imum)?"
    r"|max(?:imum)?\s*(?:of\s+)?(?P<b>\d{1,5})\s*(?:chars?|characters?)"
    r"|(?:at\s+most|up\s+to)\s+(?P<c>\d{1,5})\s*(?:chars?|characters?)",
    re.IGNORECASE,
)


def _origin_of(url: str) -> str:
    """scheme://host[:port] of an endpoint URL — the cap-cache key."""
    m = re.match(r"[a-z][a-z0-9+.-]*://[^/?#]+", url, re.IGNORECASE)
    return m.group(0).lower() if m else url


def _cap_from_error(detail: str) -> int | None:
    """The input cap a server states in its own rejection ("(48 chars
    max)", "at most 4096 characters"), or None when it states no number."""
    m = _CAP_RE.search(detail)
    if not m:
        return None
    n = int(next(g for g in (m.group("a"), m.group("b"), m.group("c")) if g))
    return n if n > 0 else None


def _merge_wavs(chunks: list[bytes]) -> bytes:
    """Concatenate same-server WAV bytes into one WAV (stdlib wave). A
    channels/width/rate disagreement is a broken server, surfaced — never
    glued into garbage audio."""
    if len(chunks) == 1:
        return chunks[0]
    # Decode and validate EVERY piece before the writer opens: a piece
    # failing inside a live writer would leave it param-less, and the
    # writer's own close-time wave.Error would mask the clean answer.
    params = None
    frames_list: list[bytes] = []
    for i, data in enumerate(chunks):
        try:
            with wave.open(io.BytesIO(data), "rb") as r:
                p = r.getparams()
                frames = r.readframes(r.getnframes())
        except (wave.Error, EOFError) as e:
            # A piece that is not decodable WAV is a broken server, not
            # silence - surfaced, never glued into garbage audio.
            raise RemoteTTSError(
                f"remote TTS split piece {i + 1} is not valid WAV audio ({e})"
            ) from e
        if params is None:
            params = p
        elif p[:3] != params[:3]:
            raise RemoteTTSError(
                "remote TTS split pieces disagree on audio format "
                f"(piece {i + 1}: {p.nchannels}ch/{p.sampwidth * 8}-bit/"
                f"{p.framerate}Hz vs {params.nchannels}ch/"
                f"{params.sampwidth * 8}-bit/{params.framerate}Hz)"
            )
        frames_list.append(frames)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        for frames in frames_list:
            w.writeframes(frames)
    return out.getvalue()


async def _post_remote(
    text: str,
    url: str,
    model: str,
    voice: str,
    speed: float | None,
    headers: dict,
) -> bytes:
    """One unwrapped POST with the #205 retry posture (a 4xx is final; one
    retry for timeouts/5xx). No splitting here — synthesize_remote owns
    the adaptive-cap logic."""
    body: dict = {"model": model, "input": text, "voice": voice, "response_format": "wav"}
    if speed is not None:
        body["speed"] = speed
    last_err: str | None = None
    timed_out = False
    for _ in range(REMOTE_ATTEMPTS):
        try:
            timeout = httpx.Timeout(REMOTE_TIMEOUT, connect=REMOTE_CONNECT_TIMEOUT)
            async with httpx.AsyncClient(timeout=timeout) as client:
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
        except httpx.ConnectTimeout as e:
            # Never connected (review, #449): the host never answered
            # the handshake. That is a connection failure (dead host /
            # silent firewall), not a slow synthesis - say so.
            timed_out = False
            last_err = f"connection failed: {e}"
        except httpx.TimeoutException as e:
            # A timeout is NOT a connection failure (#448): the server took
            # the request and is still working (a CPU-served model can run
            # minutes per request) — say so, or the user debugs a live box.
            timed_out = True
            last_err = (
                f"remote TTS timed out after {REMOTE_TIMEOUT:.0f}s (server still "
                f"synthesizing?): {e}"
            )
        except httpx.TransportError as e:
            timed_out = False
            last_err = f"connection failed: {e}"
    err = RemoteTTSError(last_err or "remote TTS failed")
    err.timed_out = timed_out
    raise err


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

    If the server rejects the input as too long (see the splitting section
    above), the chunk re-splits at the server's own cap and the pieces come
    back merged as one WAV; a piece that STILL fails surfaces the server's
    answer and drops the whole chunk, like any other failure.

    Error posture (the spec's contract): a 4xx is the server's final
    answer — one request, no retry. Timeouts and 5xx get one retry, then
    RemoteTTSError carries the server's message. An empty 200 body is a
    broken server, not silence. The endpoint maps failures to 502; the
    frontend skips the chunk and keeps the text visible.
    """
    url = _remote_url(endpoint)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    origin = _origin_of(url)
    cap = _REMOTE_CAPS.get(origin)
    detail = f"HTTP 400: input too long ({cap} chars max)" if cap else ""

    async def pieces_to_wav(parts: list[str]) -> bytes:
        wavs = [await _post_remote(p, url, model, voice, speed, headers) for p in parts]
        return _merge_wavs(wavs)

    # A cap learned earlier for THIS server pre-splits oversized chunks, so
    # only the first-ever oversized chunk pays the rejected-request tax.
    # Otherwise the chunk goes verbatim (the #205 contract for servers with
    # no practical cap), and a "too long" rejection teaches us the cap.
    if cap is None or len(text) <= cap:
        try:
            return await _post_remote(text, url, model, voice, speed, headers)
        except RemoteTTSError as e:
            detail = str(e)
            if not _SPLITTABLE_ERR.match(detail) or not _TOO_LONG_RE.search(detail):
                raise  # timeouts, bad voice/model: anything but a length rejection
            stated = _cap_from_error(detail)
            if stated is not None:
                cap = stated  # the server's own number is authoritative
            elif len(text) >= 4:
                cap = len(text) // 2  # nothing stated: start halving
            else:
                raise  # too short to halve and no number stated: answer stands

    # A "too long" 4xx - the server's own admission that `input` is over
    # its cap. Re-split at the cap and synthesize the pieces in order. A
    # piece that STILL answers too-long refines the cap further (a stated
    # number is exact; the no-number halving guess can land high). Any
    # OTHER failure is final: the server's answer surfaces and the whole
    # chunk drops - no partial audio, no unbounded shrinking.
    while True:
        if cap < 2 or cap >= len(text):
            raise RemoteTTSError(detail)  # a split cannot shrink this; answer stands
        _REMOTE_CAPS[origin] = cap
        pieces = split_sentences(text, min_len=cap, max_len=cap)
        if len(pieces) < 2:
            raise RemoteTTSError(detail)  # no usable boundary; answer stands
        try:
            return await pieces_to_wav(pieces)
        except RemoteTTSError as e:
            piece_detail = str(e)
            if not _SPLITTABLE_ERR.match(piece_detail) or not _TOO_LONG_RE.search(
                piece_detail
            ):
                raise
            stated = _cap_from_error(piece_detail)
            # Only a real refinement (a SMALLER bound) is taken; a lying or
            # repeating number falls back to halving so the loop terminates.
            cap = stated if stated is not None and stated < cap else cap // 2
            detail = piece_detail  # the answer that stands if refinement stalls


# ---- MOSS job API (the owner-run MOSS-TTS server, #443) ----------------------
# A THIRD narration flavor: the remote MOSS-TTS server on herp.local is NOT
# OpenAI-compatible - it speaks an async job API (POST a form, poll a jobs
# map, download the WAV). Verified against its live OpenAPI, 2026-10-18:
#
#   POST {base}/api/moss/generate   form: text, lang="en", ref="", wait=false,
#                                   allow_swap=true
#     wait=true  -> 200 audio/wav INLINE (24 kHz 16-bit mono)
#     wait=false -> {"job_id": "...", "status": "queued"}
#   GET  {base}/api/moss/jobs       -> {jid: {status, text, lang, ref, secs,
#                                   wav: "/api/moss/jobs/<jid>/download",
#                                   error, ...}}  (a MAP - no single-job GET)
#   GET  .../jobs/{jid}/download    -> the WAV bytes
#   DELETE .../jobs/{jid}           -> {}
#   GET  {base}/api/moss/refs       -> [{"name": "ardy", "secs": 42.0}, ...]
#   GET  {base}/api/moss/health     -> {"ok": true, ..., "ready": true}
#
# Unknown ref -> 404 (probe-verified): a stale ref name fails loudly, so the
# Settings picker sends only names the refs list returned, or "" for
# reference-less synthesis. POST bodies are urlencoded forms (FastAPI), the
# response is either the raw WAV or a small JSON envelope - never wrapped.

MOSS_WAIT_BUDGET = 600.0  # wait=true POST budget: server-side synthesis time
# IS the point of wait=true (seconds per chunk on an 8B model), so the
# per-request cap is far above the OpenAI flavor's REMOTE_TIMEOUT.
MOSS_POLL_INTERVAL = 0.5
MOSS_POLL_TIMEOUT = 600.0  # submit->poll->download ceiling (same budget)


def moss_url(base: str, path: str) -> str:
    """{base}{path} with the same base tolerance as _remote_url: a bare
    host, a trailing slash, or a pasted full path all work. MOSS paths are
    rooted at /api/moss and are never /v1-suffixed."""
    trimmed = (base or "").strip().rstrip("/")
    if not trimmed:
        raise RemoteTTSError(
            "MOSS TTS selected but no endpoint configured (Settings \u2192 Voice)",
            code="not-configured",
        )
    if "/api/moss" in trimmed:
        # A user pasted a full MOSS path; keep everything up to /api/moss.
        return trimmed.split("/api/moss", 1)[0] + path
    return trimmed + path


async def synthesize_moss(
    text: str,
    endpoint: str,
    lang: str = "en",
    ref: str = "",
    voice: str = "",
) -> bytes:
    """One chunk through the MOSS job API; returns the server's WAV bytes.

    Fast path: POST generate?wait=true returns the WAV inline (probe-verified
    ~1 s synthesis for a short probe on the live server). When the server
    answers the wait form with a job envelope instead, the job is already
    submitted, so we poll/download directly without re-submitting. The slow
    path - submit (wait=false), poll the /jobs MAP (there is no single-job
    GET), download, DELETE - is used when the wait form is explicitly
    rejected (422/5xx). The job is deleted after a successful download
    (jobs persist on the server; narration chunks would pile up otherwise).

    Error posture mirrors synthesize_remote: 4xx is final (no retry), 5xx /
    transport errors retry once, RemoteTTSError carries a user-facing
    message; the frontend skips the failed chunk and the text stays visible.
    `voice` is accepted as a ref-clip alias (the MOSS flavor's "voice" IS a
    ref clip; the saved tts_voice rides the ordinary voice channel).
    """
    url = moss_url(endpoint, "/api/moss/generate")
    ref_name = ref or voice
    form = {"text": text, "lang": (lang or "en").strip() or "en", "wait": "true"}
    if ref_name:
        form["ref"] = ref_name

    last_err: str | None = None
    for _ in range(REMOTE_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=MOSS_WAIT_BUDGET) as client:
                res = await client.post(url, data=form)
                if res.status_code == 200 and "audio" in (
                    res.headers.get("content-type") or ""
                ):
                    audio = res.content
                    if not audio:
                        raise RemoteTTSError("MOSS server returned an empty response")
                    return audio
                if res.status_code == 200:
                    try:
                        inline = res.json()
                    except ValueError:
                        inline = None
                    if isinstance(inline, dict) and str(inline.get("job_id") or ""):
                        # A server that ignores wait=true still answers the
                        # job envelope: the job already exists server-side,
                        # so pass its id to skip a duplicate submit.
                        pre_jid = str(inline["job_id"])
                        try:
                            return await _moss_job_flow(
                                client, url, text, lang, ref_name, job_id=pre_jid
                            )
                        except (
                            httpx.TimeoutException,
                            httpx.TransportError,
                        ) as e:
                            raise RemoteTTSError(f"connection failed: {e}") from None
                    raise RemoteTTSError(_remote_error_detail(res))
                if res.status_code < 500 and res.status_code != 422:
                    # A 4xx other than a wait-form mismatch (422) is the
                    # server's FINAL answer - a bad ref (404), bad auth,
                    # a malformed form. Retrying or falling back would
                    # just repeat it; surface the detail now.
                    raise RemoteTTSError(_remote_error_detail(res))
                # Wait rejected as a form mismatch (422), or a server
                # error (5xx): fall back to the async job flow on the
                # SAME client/budget. Its failures - transport ones
                # included - are terminal: a retried job flow would
                # synthesize the chunk twice.
                try:
                    return await _moss_job_flow(client, url, text, lang, ref_name)
                except (
                    httpx.TimeoutException,
                    httpx.TransportError,
                ) as e:
                    raise RemoteTTSError(f"connection failed: {e}") from None
        except RemoteTTSError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = f"connection failed: {e}"
    raise RemoteTTSError(last_err or "MOSS TTS failed")


async def _moss_job_flow(
    client: httpx.AsyncClient,
    generate_url: str,
    text: str,
    lang: str,
    ref: str,
    job_id: str | None = None,
) -> bytes:
    """Submit (when no job_id) -> poll the jobs map -> download -> delete.
    Polled via the MAP endpoint: the server has no single-job GET. When
    `job_id` is provided, the caller has already submitted the job (the
    wait=true envelope), so no duplicate synthesis is created."""
    import asyncio

    base = generate_url.rsplit("/api/moss", 1)[0]
    if job_id:
        jid = job_id
    else:
        form = {"text": text, "lang": (lang or "en").strip() or "en", "wait": "false"}
        if ref:
            form["ref"] = ref
        res = await client.post(generate_url, data=form)
        if res.status_code != 200:
            raise RemoteTTSError(_remote_error_detail(res))
        try:
            body = res.json()
        except ValueError:
            raise RemoteTTSError("MOSS server returned a non-JSON job answer") from None
        if not isinstance(body, dict) or not str(body.get("job_id") or ""):
            raise RemoteTTSError("MOSS server accepted the job but returned no job id")
        jid = str(body["job_id"])
    jobs_url = moss_url(base, "/api/moss/jobs")
    dl_url = f"{base}/api/moss/jobs/{jid}/download"
    waited = 0.0
    while True:
        await asyncio.sleep(MOSS_POLL_INTERVAL)
        waited += MOSS_POLL_INTERVAL
        if waited > MOSS_POLL_TIMEOUT:
            raise RemoteTTSError(
                f"MOSS synthesis timed out after {int(MOSS_POLL_TIMEOUT)} s (job {jid})"
            )
        res = await client.get(jobs_url)
        if res.status_code != 200:
            raise RemoteTTSError(_remote_error_detail(res))
        try:
            jobs = res.json()
        except ValueError:
            raise RemoteTTSError("MOSS server returned a non-JSON jobs answer") from None
        job = jobs.get(jid) if isinstance(jobs, dict) else None
        if job is None:
            continue  # not registered yet - the map is eventual
        status = str(job.get("status") or "")
        if status == "done":
            break
        if status == "error":
            raise RemoteTTSError(str(job.get("error") or "MOSS job failed"))
        # queued / running / anything else: keep polling
    res = await client.get(dl_url)
    if res.status_code != 200:
        raise RemoteTTSError(_remote_error_detail(res))
    audio = res.content
    if not audio:
        raise RemoteTTSError("MOSS server returned an empty audio download")
    # Cleanup is best-effort: a failed DELETE must not fail the narration.
    try:
        await client.delete(f"{base}/api/moss/jobs/{jid}")
    except httpx.HTTPError:
        pass
    return audio


async def probe_moss_refs(endpoint: str, api_key: str = "") -> list[str]:
    """The reference clips the MOSS server offers (the MOSS "voice" IS a
    ref clip - zero-shot cloning). [] on ANY failure: the picker falls back
    to free-text, mirroring probe_remote_voices' posture."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=VOICES_TIMEOUT) as client:
            res = await client.get(moss_url(endpoint, "/api/moss/refs"), headers=headers)
    except (httpx.TimeoutException, httpx.TransportError):
        return []
    if res.status_code != 200:
        return []
    try:
        body = res.json()
    except ValueError:
        return []
    names: list[str] = []
    if isinstance(body, list):
        for item in body:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(item["name"])
            elif isinstance(item, str):
                names.append(item)
    return list(dict.fromkeys(n.strip() for n in names if n.strip()))


async def probe_moss_health(endpoint: str, api_key: str = "") -> bool:
    """True when the server answers health with ready/ok truthy. ANY
    failure reads False - status only uses it for a soft availability hint."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=VOICES_TIMEOUT) as client:
            res = await client.get(moss_url(endpoint, "/api/moss/health"), headers=headers)
    except (httpx.TimeoutException, httpx.TransportError):
        return False
    if res.status_code != 200:
        return False
    try:
        body = res.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    return bool(body.get("ready", body.get("ok", False)))


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


async def probe_remote_models(endpoint: str, api_key: str = "") -> list[str]:
    """The model ids a server lists at the STANDARD OpenAI GET /v1/models
    (MOSS-TTS-Realtime serves it; Kokoro-shaped boxes may not). Same
    posture as voices: ANY failure reads as "no list" — discovery is a
    nicety, never a requirement."""
    trimmed = (endpoint or "").strip().rstrip("/")
    if not trimmed:
        return []
    base = trimmed[:-3] if trimmed.endswith("/v1") else trimmed
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=VOICES_TIMEOUT) as client:
            res = await client.get(base + "/v1/models", headers=headers)
    except (httpx.TimeoutException, httpx.TransportError):
        return []
    if res.status_code != 200:
        return []
    try:
        body = res.json()
    except ValueError:
        return []
    return _normalize_models(body)


def _normalize_models(body) -> list[str]:
    """Model ids from an OpenAI list-shape body ({"data": [{"id": ...}]},
    the MOSS box's shape), {"models": [...]}, or a bare array of ids
    (plain strings or {"id": ...} objects) - #448's spec asks for all
    three dialects. Voices cannot leak in here: this only ever runs on
    the /v1/models answer, never on a shared endpoint."""
    if isinstance(body, list):
        # Bare-array dialect (#448 spec): ids as plain strings or as
        # OpenAI-shaped objects. Junk entries are dropped, the rest
        # trimmed + deduplicated, server order preserved.
        ids = []
        for v in body:
            i = v.get("id") if isinstance(v, dict) else v
            if isinstance(i, str) and i.strip():
                ids.append(i.strip())
        return list(dict.fromkeys(ids))
    if not isinstance(body, dict):
        return []
    ids: list[str] = []
    if isinstance(body.get("data"), list):
        # OpenAI list shape: entries are objects with an "id" - bare
        # strings are junk and dropped.
        for v in body["data"]:
            i = v.get("id") if isinstance(v, dict) else None
            if isinstance(i, str) and i.strip():
                ids.append(i.strip())
    elif isinstance(body.get("models"), list):
        # The looser dialect: plain name strings.
        ids = [v.strip() for v in body["models"] if isinstance(v, str) and v.strip()]
    return list(dict.fromkeys(ids))


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


def heuristic_briefing(md: str, max_chars: int = SAY_MAX_CHARS) -> str:
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
    # Walk sentence boundaries; the LAST sentence starts where the previous
    # match ended. (Slicing from the last punctuation index alone would
    # collapse the closing to bare punctuation like ".".)
    prev = None
    last = None
    for m in _SENT_END_ANY.finditer(tail):
        prev = last
        last = m
    if last is None:
        closing = tail
    else:
        # Closing = the LAST sentence only. Slicing tail[: last.end(1)]
        # (from the paragraph's START to the last sentence end) spoke the
        # whole multi-sentence paragraph back — the read-aloud repeated
        # the message nearly verbatim.
        closing = tail[prev.end() if prev is not None else 0 : last.end(1)].strip()
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


def spoken_line(
    briefing: str | None, md: str, max_chars: int = SAY_MAX_CHARS, kokoro: bool | None = None
) -> str:
    """The final TTS input for a turn: the model-emitted briefing when
    usable, else the heuristic, else the old truncated verbatim prose (the
    fallback is today's behavior, never silence). Everything passes through
    prose_for_speech so no markdown junk reaches the synthesizer, and the
    hard cap is enforced here — in one place.

    #451: kokoro=None consults kokoro_voice(); a non-Kokoro voice gets the
    engine-neutral cleanup only."""
    def _finish(text: str) -> str:
        # #206: the speech-normalization pass is the last thing a briefing
        # receives before the synthesizer. The cap is re-enforced after it:
        # spelled-out numbers ("four four three") are longer than digits.
        out = normalize_for_speech(text, kokoro=kokoro)
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


# Currency amounts (#292): $-glued only, optional thousands commas and
# optional two-digit cents. Runs before the year pass so "$1999" reads as
# money, not as a year with a surviving "$". The (?!\.?\d) tail mirrors
# _NOT_FRAG: no glued letters ("$5th" is not an amount), no fragment
# cents ("$19.99" at a sentence end still matches; "$19.999" / "$19.9"
# do not), and at most three comma groups (the words stop at billions).
_CURRENCY = re.compile(
    r"(?<![\w.])\$([1-9]\d{0,2}(?:,\d{3}){1,3}|\d+)(?:\.(\d{2}))?(?!\.?\d)(?![A-Za-z])")

_MAGNITUDE = ("", " thousand", " million", " billion")


def _under_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    if not hundreds:
        return _under_hundred(rest)
    out = _ONES[hundreds] + " hundred"
    return f"{out} {_under_hundred(rest)}" if rest else out


def _cardinal_words(n: int) -> str:
    """Cardinal for the dollars part of an amount (1299 -> one thousand
    two hundred ninety-nine). Humans say twelve hundred too; the grouped
    written form decomposes by magnitude, so the words follow the commas."""
    if n == 0:
        return "zero"
    parts: list[str] = []
    for mag in _MAGNITUDE:
        n, chunk = divmod(n, 1000)
        if chunk:
            parts.append(_under_thousand(chunk) + mag)
        if not n:
            break
    return " ".join(reversed(parts))


def _cents_plain(cc: str) -> str:
    """The cents pair the way prices are said: plain two-digit (99 ->
    ninety-nine), oh-padded below ten (05 -> oh five)."""
    n = int(cc)
    if n == 0:
        return ""
    return f"oh {_DIGITS[str(n)]}" if n < 10 else _under_hundred(n)


def _currency_words(m: "re.Match") -> str:
    dollars = int(m.group(1).replace(",", ""))
    cc = m.group(2)
    cents_val = int(cc) if cc is not None else 0
    if not dollars:
        # Sub-dollar: read in cents with the unit (75 -> seventy-five cents).
        if not cents_val:
            return "zero dollars"
        return "one cent" if cents_val == 1 else _under_hundred(cents_val) + " cents"
    # With cents, units elide the way humans say prices: nineteen
    # ninety-nine, not nineteen dollars and ninety-nine cents. Whole
    # amounts keep the unit (five dollars).
    if cents_val:
        return f"{_cardinal_words(dollars)} {_cents_plain(cc or '0')}"
    return _cardinal_words(dollars) + (" dollar" if dollars == 1 else " dollars")


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


def kokoro_voice_cfg(voice: dict) -> bool:
    """#451: the Kokoro-shape gate over an already-loaded voice config.
    Local synthesis is always kokoro; a remote engine is Kokoro-family
    only when its model name says so (the moss endpoint, api.openai.com
    tts-1, and every other front-end read written forms natively)."""
    if (voice.get("tts_engine") or "local") != "remote":
        return True
    # An empty remote model name rides the same default _remote_engine_args
    # sends ("kokoro"), so the classification always matches synthesis.
    return "kokoro" in (voice.get("tts_model") or "kokoro").lower()


def kokoro_voice() -> bool:
    """#451: kokoro_voice_cfg over the SAVED voice config, read on every
    call so a Settings change applies without a restart. A config failure
    reads non-kokoro (skip the Kokoro-only shaping rather than feed a
    non-espeak engine text it stutters on)."""
    try:
        return kokoro_voice_cfg(load_config().get("voice") or {})
    except Exception:
        return False


def normalize_for_speech(text: str, *, kokoro: bool | None = None) -> str:
    """Deterministic written→spoken pass over TTS INPUT only (the chat
    transcript is untouched). Order matters: markers/SSML first so their
    digits never feed the number rules, then years before bare runs, then
    versionish dotted groups, then long digit runs by context (phones
    group, everything else digit-by-digit), then short numbers, then
    acronyms.

    #451: everything after stage 1 is Kokoro-shaping — it pre-speaks
    exactly what non-espeak engines (moss-tts) already read natively, and
    the letter-spaced acronyms make those engines stutter. kokoro=None
    consults kokoro_voice(); False runs engine-neutral cleanup only."""
    if not text:
        return text
    if kokoro is None:
        kokoro = kokoro_voice()

    # 1. SSML tags and emotion markers cannot survive into synthesis.
    t = _SSML_TAG.sub(" ", text)
    t = _EMOTION_MARKER.sub(
        lambda m: "" if _is_emotion_marker(m.group(1)) else m.group(0), t,
    )
    t = re.sub(r"  +", " ", t)
    t = re.sub(r" ([,.!?;:])", r"\1", t)
    if not kokoro:
        # #451: non-Kokoro voice — skip the written→spoken shaping below.
        return t.strip()

    # 1.5 Currency amounts ($19.99, $1,299.50, $5) before the year pass:
    # money is never a year, and the $ must not survive into synthesis.
    t = _CURRENCY.sub(_currency_words, t)

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
