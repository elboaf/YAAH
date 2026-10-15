"""Image persistence: data URLs and raw bytes <-> files under
IMAGES_ROOT (dev: backend/data/images; packaged: ~/.yaah/images - the
PyInstaller _MEI extraction dir `__file__` points at vanishes on exit).

Only relative paths are stored in the database; the bytes live on disk so
the DB stays small. Paths are posix-style relative to IMAGES_ROOT and
double as the public URL fragment (/api/images/<rel>) AND the handle the
agent passes to generate_image / upscale_image / view_image.
"""
import base64
import os
import re
import sys
import uuid
from pathlib import Path


def _images_root() -> Path:
    """Where stored images live.

    `YAAH_IMAGES_DIR` wins (tests, portable setups). Frozen builds (the
    packaged app is a PyInstaller --onefile bundle) persist under
    ~/.yaah/images - mirroring config/database._default_data_dir, because
    `__file__` here points into the throwaway _MEI extraction dir that
    vanishes on exit: pasted chat images written there were undiscoverable
    (a hunt through Temp_MEIxxxx) AND silently lost on app exit. Dev
    runs keep the repo-local backend/data/images.
    """
    env = os.environ.get("YAAH_IMAGES_DIR")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        home = Path.home() / ".yaah" / "images"
        home.mkdir(parents=True, exist_ok=True)
        return home
    return Path(__file__).parent.parent / "data" / "images"


IMAGES_ROOT = _images_root()

_DATA_URL_RE = re.compile(
    r"^data:image/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=\s]+)$"
)

_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


def _ext_for(mime: str) -> str:
    return "jpg" if mime in ("jpeg", "image/jpeg") else mime.replace("image/", "")


def save_data_url(data_url: str, subdir: str = "") -> str | None:
    """Decode a data URL to a file under IMAGES_ROOT; return its rel path
    (posix, relative) or None if the string is not an image data URL."""
    m = _DATA_URL_RE.match(data_url or "")
    if not m:
        return None
    raw = base64.b64decode(m.group(2))
    return save_bytes(raw, _ext_for(m.group(1)), subdir)


def save_bytes(raw: bytes, ext: str, subdir: str = "") -> str:
    """Write image bytes to a fresh file; return the rel path."""
    folder = IMAGES_ROOT / subdir if subdir else IMAGES_ROOT
    folder.mkdir(parents=True, exist_ok=True)
    rel = f"{subdir}/{uuid.uuid4().hex}.{ext}" if subdir else f"{uuid.uuid4().hex}.{ext}"
    (IMAGES_ROOT / rel).write_bytes(raw)
    return rel.replace("\\", "/")


def load_data_url(rel: str) -> str | None:
    """Read a stored image back as a data URL (for the LLM), or None."""
    p = (IMAGES_ROOT / rel).resolve()
    root = IMAGES_ROOT.resolve()
    if not (p == root or root in p.parents) or not p.is_file():
        return None
    ext = p.suffix.lstrip(".").lower()
    mime = _MIME_BY_EXT.get(ext)
    if not mime:
        return None
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"
