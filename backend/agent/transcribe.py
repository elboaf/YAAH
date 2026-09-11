"""Speech-to-text for voice prompting.

Two engines, chosen in config (`voice.engine`):

- local (default): a pre-packaged whisper.cpp CLI binary + ggml model.
  Fully on-device; audio never leaves the machine.
- cloud: the user's own OpenAI-compatible /audio/transcriptions endpoint
  (OpenAI whisper-1, Groq whisper-large-v3, ...), BYOK.

Resolution is deliberately sidecar-agnostic: the packaged app drops the
whisper binary under <resource dir>/whisper/bin and the model under
<resource dir>/whisper/models next to backend.exe, dev runs fall back to
the repo layout, and YAAH_WHISPER_BIN / YAAH_WHISPER_MODEL override
everything (used by tests and by anyone pointing at their own build).
"""
import os
import sys
import subprocess
import tempfile
from pathlib import Path

# Model file names we know how to prefer, best bundled candidate first.
PREFERRED_MODELS = (
    "ggml-base-q5_1.bin",  # pre-packaged in installers
    "ggml-base.en.bin",
    "ggml-base.bin",
    "ggml-tiny-q5_1.bin",
    "ggml-small-q5_1.bin",
)

_LOCAL_TIMEOUT = 300  # seconds; small models on CPU do ~10x realtime


def _exe_dir() -> Path:
    """Directory of the running interpreter: backend.exe's folder when
    frozen (PyInstaller one-file/one-dir) or backend/ in dev."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent  # backend/


def _binary_dirs() -> list[Path]:
    dirs = []
    if os.environ.get("YAAH_WHISPER_BIN"):
        dirs.append(Path(os.environ["YAAH_WHISPER_BIN"]))
    dirs += [
        _exe_dir() / "whisper" / "bin",  # packaged: next to backend.exe
        # packaged: Tauri resources land under the spawn cwd (lib.rs picks
        # the dir containing backend/main.py), so whisper rides along there.
        Path.cwd() / "backend" / "whisper" / "bin",
        Path.cwd() / "whisper" / "bin",
        Path(__file__).parent.parent / "whisper" / "bin",  # repo checkout
        Path(__file__).parent.parent / "bin" / "Release",  # dev: windows zip
        Path(__file__).parent.parent / "bin",  # dev: linux/macos build
    ]
    return dirs


def _model_dirs() -> list[Path]:
    dirs = []
    if os.environ.get("YAAH_WHISPER_MODEL"):
        dirs.append(Path(os.environ["YAAH_WHISPER_MODEL"]).parent)
    dirs += [
        _exe_dir() / "whisper" / "models",  # packaged: next to backend.exe
        Path.cwd() / "backend" / "whisper" / "models",  # packaged: resources
        Path.cwd() / "whisper" / "models",
        Path(__file__).parent.parent / "whisper" / "models",  # repo checkout
        Path(__file__).parent.parent / "data" / "models",  # dev
    ]
    return dirs


def find_binary() -> Path | None:
    """Locate a whisper.cpp CLI (whisper-cli / main, .exe on Windows)."""
    names = ("whisper-cli", "main")
    for d in _binary_dirs():
        if not d.is_dir():
            continue
        for name in names:
            for ext in (".exe", ""):
                p = d / f"{name}{ext}"
                if p.is_file():
                    return p
    return None


def find_model() -> Path | None:
    """Best bundled/downloaded ggml model: preferred names first, then any
    ggml-*.bin the user dropped into a model dir."""
    for name in PREFERRED_MODELS:
        for d in _model_dirs():
            p = d / name
            if p.is_file():
                return p
    for d in _model_dirs():
        if d.is_dir():
            for p in sorted(d.glob("ggml-*.bin")):
                return p
    return None


def local_available() -> bool:
    return find_binary() is not None and find_model() is not None


def transcribe_local(wav_path: str) -> str:
    """Run whisper.cpp on a 16 kHz mono WAV; return the transcript text."""
    binary = find_binary()
    model = find_model()
    if not binary or not model:
        raise RuntimeError(
            "Local transcription is not available (whisper binary or model not found)"
        )
    cmd = [
        str(binary),
        "-m",
        str(model),
        "-f",
        str(wav_path),
        "-nt",  # no timestamps — plain text for the composer
        "-np",  # no progress prints on stderr
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=_LOCAL_TIMEOUT
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"whisper failed ({proc.returncode}): {detail[-1] if detail else 'no output'}"
        )
    return proc.stdout.strip()


async def transcribe_cloud(wav_path: str, endpoint: str, api_key: str, model: str) -> str:
    """POST the WAV to an OpenAI-compatible /audio/transcriptions endpoint."""
    import httpx

    if not endpoint or not api_key:
        raise RuntimeError("Cloud transcription needs an endpoint and API key in Settings")
    url = endpoint.rstrip("/")
    if not url.endswith("/audio/transcriptions"):
        url += "/audio/transcriptions"
    async with httpx.AsyncClient(timeout=120) as client:
        with open(wav_path, "rb") as f:
            res = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": ("audio.wav", f, "audio/wav")},
                data={"model": model or "whisper-1"},
            )
    if res.status_code != 200:
        raise RuntimeError(f"Cloud transcription failed ({res.status_code}): {res.text[:200]}")
    return (res.json().get("text") or "").strip()


def save_wav(pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
    """Wrap raw 16-bit mono PCM in a WAV file for the engines."""
    import wave

    if not pcm16_bytes:
        raise ValueError("Recording contained no audio")
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16_bytes)
    return path
