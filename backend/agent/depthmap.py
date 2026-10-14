"""Depth maps for depth-controlled generation (#403).

sd.cpp has no depth model, and refcontrol-style LoRAs need a depth map
as the FIRST reference image. This module derives one locally:

- Neural path: DepthAnything-V2-small via onnxruntime (OPTIONAL dep -
  not in requirements.txt). The ONNX model (~100MB) is downloaded once
  to ~/.yaah/models/ on first use.
- Fallback: a luminance + box-blur approximation. Structurally wrong as
  depth (brightness is not distance) but free, instant, and better than
  nothing: it still separates subject from background, which is the
  coarse signal the depth-control LoRA keys on.

Never raises: the public API returns (png_bytes, note) and only fails
(png_bytes=None) when the input cannot be decoded at all.

Threading: download and ONNX inference are BLOCKING (hundreds of MB,
seconds of CPU). compute_depth() is the only async entry point and it
shunts all of that to a worker thread - the backend's event loop must
never stall under a generating agent.
"""

import asyncio
import io
import os
import tempfile
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------
# Model download and caching
# ---------------------------------------------------------------------

_MODEL_DIR = Path.home() / ".yaah" / "models"
_MODEL_PATH = _MODEL_DIR / "depth-anything-v2-small.onnx"
_MODEL_URL = (
    "https://huggingface.co/Xenova/depth-anything-v2-small-hf"
    "/resolve/main/onnx/model.onnx"
)
# Long read timeout: ~100MB over a slow line must not give up at 30s.
_MODEL_TIMEOUT = (30.0, 600.0)  # (connect, read)

# Set when _ensure_model() downloaded the file this process, so the note
# can tell the user the one-time cost happened. _MODEL_BYTES caches the
# file size for the same note (stat-per-call would be fine too, but the
# download path already knows the number).
_downloaded_this_process = False
_MODEL_BYTES = 0


def _model_exists() -> bool:
    """Is the ONNX model already cached on disk?"""
    return _MODEL_PATH.exists()


def _ensure_model() -> bool:
    """Download the ONNX model if missing. Returns True when the file is
    present afterwards. Any failure returns False (the caller falls back
    to the luminance approximation) - a missing model must never raise
    out of a generation request."""
    global _downloaded_this_process, _MODEL_BYTES
    if _model_exists():
        return True
    try:
        import httpx

        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        # *.part then os.replace: a killed download must never leave a
        # truncated file at the real path (the session would load it and
        # fail confusingly on every later call). SYNC client on purpose:
        # the caller runs on a worker thread, and HF "resolve" URLs
        # redirect, which AsyncClient+stream setups must be told to
        # follow (a silent zero-byte 3xx body otherwise).
        fd, tmp_path = tempfile.mkstemp(suffix=".part", dir=str(_MODEL_DIR))
        try:
            with httpx.Client(
                timeout=_MODEL_TIMEOUT, follow_redirects=True
            ) as client:
                with client.stream("GET", _MODEL_URL) as r:
                    r.raise_for_status()
                    with os.fdopen(fd, "wb") as f:
                        for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                            f.write(chunk)
            os.replace(tmp_path, _MODEL_PATH)
            _downloaded_this_process = True
            _MODEL_BYTES = _MODEL_PATH.stat().st_size
            return True
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
    except Exception:  # noqa: BLE001 - any download failure = fallback
        return False


# ---------------------------------------------------------------------
# PIL helpers
# ---------------------------------------------------------------------

try:
    from PIL import Image

    # Pillow >= 10 moved the resampling constants into an enum; older
    # versions keep module-level aliases. Resolve once.
    _BILINEAR = (
        Image.Resampling.BILINEAR
        if hasattr(Image, "Resampling")
        else Image.BILINEAR
    )
except ImportError:  # pragma: no cover - Pillow ships in requirements
    Image = None
    _BILINEAR = None


def _decode_rgb(image_bytes: bytes):
    """Decode image bytes to an RGB PIL image. Raises on failure."""
    if Image is None:
        raise RuntimeError("Pillow is not installed")
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


def _encode_l_png(arr8, w: int, h: int) -> bytes:
    """Encode a uint8 2-D array as a PNG resized back to (w, h)."""
    img = Image.fromarray(arr8, mode="L").resize((w, h), _BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _normalize8(a):
    """Min-max stretch a float array to uint8 0..255 (flat input -> 0)."""
    lo, hi = float(a.min()), float(a.max())
    if hi - lo < 1e-6:
        return np.zeros_like(a, dtype=np.uint8)
    return ((a - lo) / (hi - lo) * 255.0).astype(np.uint8)


# ---------------------------------------------------------------------
# Neural path (DepthAnything-V2-small ONNX)
# ---------------------------------------------------------------------

_session = None  # cached InferenceSession (None until first success)


def _init_onnx_session():
    """Return a ready InferenceSession, or None when onnxruntime is
    missing / the model is unavailable. Module-level so tests can patch
    it to force the fallback path."""
    global _session
    if _session is not None:
        return _session
    try:
        import onnxruntime as ort  # optional dependency
    except ImportError:
        return None
    if not _ensure_model():
        return None
    try:
        _session = ort.InferenceSession(
            str(_MODEL_PATH), providers=["CPUExecutionProvider"]
        )
        return _session
    except Exception:  # noqa: BLE001 - corrupt/unsupported model file
        return None


def _neural_depth(sess, image_bytes: bytes):
    """Run the model. Returns (png_bytes, None) or (None, reason)."""
    try:
        img = _decode_rgb(image_bytes)
        w, h = img.size
        # The model's native input is 518x518 with ImageNet normalization.
        small = img.resize((518, 518), _BILINEAR)
        arr = np.asarray(small, dtype=np.float32) / 255.0
        arr = (arr - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / (
            np.array([0.229, 0.224, 0.225], dtype=np.float32)
        )
        arr = arr.transpose(2, 0, 1)[np.newaxis]  # 1x3xHxW
        out = sess.run(None, {sess.get_inputs()[0].name: arr})[0]
        depth = np.asarray(out).squeeze()
        if depth.ndim != 2:
            return None, f"unexpected model output shape {depth.shape}"
        return _encode_l_png(_normalize8(depth), w, h), None
    except Exception as e:  # noqa: BLE001 - fall back, never raise
        return None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------
# Fallback: luminance + 9x9 box blur (integral image)
# ---------------------------------------------------------------------

_BLUR_PAD = 4  # 9x9 window -> radius 4


def _luminance_depth(image_bytes: bytes):
    """Brightness-as-depth approximation. Returns (png_bytes, None) or
    (None, reason)."""
    try:
        img = _decode_rgb(image_bytes)
        w, h = img.size
        arr = np.asarray(img, dtype=np.float64)
        lum = 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]
        p = _BLUR_PAD
        padded = np.pad(lum, p, mode="edge")
        # Integral image: the window sum for output pixel (i, j) covers
        # rows i..i+2p and cols j..j+2p of `padded`, i.e. four corner
        # reads of the cumulative table I (which is padded by one).
        I = (
            np.pad(padded, ((1, 0), (1, 0)), mode="constant")
            .cumsum(axis=0)
            .cumsum(axis=1)
        )
        k = 2 * p + 1
        blurred = I[k:, k:] - I[:-k, k:] - I[k:, :-k] + I[:-k, :-k]
        return _encode_l_png(_normalize8(blurred), w, h), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------
# Sync worker (runs on a thread via compute_depth) + async entry point
# ---------------------------------------------------------------------

def _compute_depth_sync(image_bytes: bytes):
    """The blocking part: model download, ONNX inference, or the numpy
    fallback. Returns (png_bytes, note). Never raises for undecodable
    input (returns (None, reason) instead)."""
    sess = _init_onnx_session()
    reason = None
    if sess is not None:
        png, reason = _neural_depth(sess, image_bytes)
        if png is not None:
            note = "depth map computed locally (DepthAnything-V2-small)"
            if _downloaded_this_process:
                note = (
                    f"depth model downloaded (~{_MODEL_BYTES // (1024 * 1024)}MB, "
                    f"one-time); {note}"
                )
            return png, note
    png, fallback_reason = _luminance_depth(image_bytes)
    if png is None:
        return (
            None,
            "could not decode image for depth extraction "
            f"({fallback_reason})",
        )
    why = reason or "onnxruntime not installed or the depth model is unavailable"
    return (
        png,
        "depth map approximated from luminance (neural depth "
        f"unavailable: {why}); pose guidance is coarse - install "
        "onnxruntime for true depth",
    )


async def compute_depth(image_bytes: bytes):
    """Depth map for one image, off the event loop. Returns
    (png_bytes, note); png_bytes is None only when the input cannot be
    decoded at all."""
    return await asyncio.to_thread(_compute_depth_sync, image_bytes)
