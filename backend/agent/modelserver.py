"""Local model server manager (T6, issue #678).

Manages a local llama-server process for model inference (embeddings first,
chat later). Follows the architecture from ADR-0021 and ADR-0022:

- Download-on-demand models from an in-code manifest (version-pinned)
- CPU-first with optional GPU offload (probe for VRAM headroom)
- Single-tenant: one model per server process
- Health checks, crash restart, clean process-tree shutdown
- Out-of-box state: server OFF (no downloads, no process until enabled)

The embeddings client (T5) uses this server's endpoint when it's running.
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from backend.agent import config as config_mod

log = logging.getLogger("yaah.modelserver")

# ---------------------------------------------------------------------
# Model manifest (in-code, version-pinned, not user-editable)
#
# Each entry: {id, name, hf_repo, files, revision, sha256, license, tasks,
# dimensions, flags}. The id is the user-facing identifier stored in config.
# ---------------------------------------------------------------------

MODELS: list[dict[str, Any]] = [
    {
        "id": "qwen3-embedding-0.6b",
        "name": "Qwen3-Embedding-0.6B (GGUF)",
        "hf_repo": "Qwen/Qwen3-Embedding-0.6B-GGUF",
        # Filename is case-sensitive on HF (a wrong case 404s).
        "files": ["Qwen3-Embedding-0.6B-Q8_0.gguf"],
        # Pinned upstream commit; a new upstream version arrives via a
        # YAAH release, never by moving this pin (ADR-0022).
        "revision": "370f27d7550e0def9b39c1f16d3fbaa13aa67728",
        # sha256 of the GGUF payload (the HF LFS oid at the pinned
        # revision) - download_model refuses a mismatched file.
        "sha256": "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439",
        "license": "Apache-2.0",
        "tasks": ["embedding"],
        "dimensions": 1024,
        "flags": ["--embeddings"],
    },
]

# Default model ids per task
DEFAULT_MODEL: dict[str, str] = {"embedding": "qwen3-embedding-0.6b"}


def find_model(model_id: str) -> Optional[dict[str, Any]]:
    """Find a model by its manifest id."""
    for m in MODELS:
        if m["id"] == model_id:
            return m
    return None


def list_models(task: Optional[str] = None) -> list[dict[str, Any]]:
    """List models, optionally filtered by task."""
    if task is None:
        return MODELS
    return [m for m in MODELS if task in m["tasks"]]


def default_model(task: str) -> dict[str, Any]:
    """Get the default model for a task."""
    model_id = DEFAULT_MODEL.get(task)
    if model_id:
        m = find_model(model_id)
        if m:
            return m
    # Fall back to first model with the task
    candidates = list_models(task)
    if candidates:
        return candidates[0]
    raise ValueError(f"No default model for task '{task}'")


# ---------------------------------------------------------------------
# Model download (download-on-demand, ~/.yaah/models/)
# ---------------------------------------------------------------------

MODELS_DIR = Path.home() / ".yaah" / "models"

# Download timeout (connect, read) - large models take time
DOWNLOAD_TIMEOUT = (30.0, 600.0)


class DownloadTracker:
    """Track download progress for large model files."""

    def __init__(self, total_size: Optional[int] = None):
        self.total_size = total_size
        self.bytes_downloaded = 0

    def update(self, bytes_downloaded: int) -> None:
        self.bytes_downloaded = bytes_downloaded

    @property
    def progress(self) -> float:
        if self.total_size and self.total_size > 0:
            return min(1.0, self.bytes_downloaded / self.total_size)
        return 0.0


def ensure_model_dir(path: Path) -> None:
    """Create the model download directory if it doesn't exist."""
    path.mkdir(parents=True, exist_ok=True)


def _model_download_url(entry: dict[str, Any], filename: str) -> str:
    """Build the Hugging Face download URL for a model file."""
    repo = entry["hf_repo"]
    revision = entry.get("revision", "main")
    return f"https://huggingface.co/{repo}/resolve/{revision}/{filename}"


def download_model(entry: dict[str, Any], progress_cb: Optional[Callable] = None) -> Path:
    """Download a model from the manifest entry. Returns the path to the
    downloaded file.

    Resumable: partial bytes accumulate in a deterministic
    `<filename>.part` next to the target and the next attempt continues
    with an HTTP Range request, so a dropped 600MB fetch does not start
    over. The finished file is checksum-verified (manifest sha256) and
    atomically renamed into place, so a truncated or corrupted download
    can never sit at the real path. Runs on a thread via
    asyncio.to_thread to avoid blocking the event loop.
    """
    model_id = entry["id"]
    log.info("Downloading model %s from %s", model_id, entry["hf_repo"])

    # Create model-specific directory
    model_dir = MODELS_DIR / model_id
    ensure_model_dir(model_dir)

    downloaded_files = []
    total_bytes = 0

    for filename in entry["files"]:
        target = model_dir / filename
        log.info("  Downloading %s -> %s", filename, target)

        url = _model_download_url(entry, filename)
        # Deterministic part file: the resume anchor across attempts.
        tmp_path = str(target) + ".part"

        resume_from = 0
        if os.path.exists(tmp_path):
            resume_from = os.path.getsize(tmp_path)
            log.info("  resuming %s from byte %d", filename, resume_from)

        try:
            headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
            with httpx.Client(
                timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
            ) as client:
                with client.stream("GET", url, headers=headers) as r:
                    # A server that ignores Range replies 200 with the
                    # whole file: restart from zero rather than append.
                    if resume_from and r.status_code != 206:
                        resume_from = 0
                        r.raise_for_status()
                    else:
                        r.raise_for_status()
                    content_length = r.headers.get("Content-Length")
                    tracker = DownloadTracker()
                    if content_length:
                        tracker.total_size = resume_from + int(content_length)

                    mode = "ab" if resume_from else "wb"
                    with open(tmp_path, mode) as f:
                        chunk_num = 0
                        for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                            if not chunk:
                                break
                            f.write(chunk)
                            tracker.bytes_downloaded = (
                                resume_from + f.tell()
                            )
                            total_bytes += len(chunk)
                            chunk_num += 1
                            if progress_cb and chunk_num % 10 == 0:
                                progress_cb(tracker)

            # Verify checksum if available
            if entry.get("sha256"):
                sha = hashlib.sha256()
                with open(tmp_path, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        sha.update(chunk)
                computed = sha.hexdigest()
                if computed != entry["sha256"]:
                    # Complete but corrupt: the bytes themselves are bad,
                    # so the anchor must not survive to poison the next
                    # resume.
                    os.remove(tmp_path)
                    raise ValueError(f"Checksum mismatch for {filename}: "
                                     f"expected {entry['sha256']}, got {computed}")

            # Atomic rename
            os.replace(tmp_path, str(target))
            downloaded_files.append(target)
            log.info("  Downloaded %s (%d bytes)", filename, target.stat().st_size)
        except Exception:
            # KEEP the part file on failure: it is the next attempt's
            # resume anchor. (The checksum branch above removes it
            # itself when a COMPLETED download is corrupt - the only
            # case where resuming would be wrong.) A truncated file can
            # never match the full-file digest, so re-hashing here
            # would delete every partial download.
            raise

    log.info("Model %s downloaded successfully (%d files, %d bytes total)",
            model_id, len(downloaded_files), total_bytes)
    return downloaded_files[0]


def is_model_downloaded(entry: dict[str, Any]) -> bool:
    """Check if a model is already downloaded."""
    model_dir = MODELS_DIR / entry["id"]
    if not model_dir.exists():
        return False
    for filename in entry["files"]:
        if not (model_dir / filename).exists():
            return False
    return True


# ---------------------------------------------------------------------
# Server lifecycle (spawn, health check, restart, shutdown)
# ---------------------------------------------------------------------

# Port range for ephemeral server ports
PORT_RANGE = (10000, 20000)

# Health check interval
HEALTH_CHECK_INTERVAL = 5.0

# Maximum restarts before giving up
MAX_RESTARTS = 3

# Create subprocess without console window on Windows
CREATE_NO_WINDOW = 0x08000000


def select_free_port() -> int:
    """Select an available port for the local model server."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def health_check(port: int) -> bool:
    """Check if the server is healthy by querying its endpoint."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"http://localhost:{port}/v1/models")
            return r.status_code == 200
    except Exception:
        return False


def _find_server_binary() -> Optional[str]:
    """Find the llama-server binary on the system."""
    # Check PATH first
    binary = shutil.which("llama-server")
    if binary:
        return binary
    # Check common install locations on Windows
    if os.name == "nt":
        for location in [
            os.path.expandvars("%LOCALAPPDATA%\\Programs\\llama.cpp\\llama-server.exe"),
            os.path.expandvars("%USERPROFILE%\\Downloads\\llama.cpp\\build\\bin\\llama-server.exe"),
        ]:
            if os.path.exists(location):
                return location
    return None


def _probe_vram_headroom() -> Optional[dict]:
    """Probe for GPU VRAM headroom.

    Returns None if no GPU or unable to probe.
    Returns {total_vram, free_vram, has_headroom} dict otherwise.
    """
    if os.name != "nt":
        return None

    try:
        import subprocess
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | Select-Object "
             "AdapterRAM,FreeVRAM"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None

        # Parse the output - this is best-effort and varies by GPU driver
        lines = result.stdout.strip().split("\n")
        for line in lines:
            if "AdapterRAM" in line or "FreeVRAM" in line:
                # This is a simplified parser; real implementation would be more robust
                pass

        # Fall back to a conservative estimate
        # TODO: Use nvidia-smi or AMD equivalents for more accurate detection
        return {
            "total_vram": 8 * 1024 * 1024 * 1024,  # Assume 8GB
            "free_vram": 4 * 1024 * 1024 * 1024,   # Assume 4GB free
            "has_headroom": True,
        }
    except Exception:
        return None


def should_use_gpu_offload() -> bool:
    """Determine if GPU offload should be used based on VRAM headroom."""
    probe = _probe_vram_headroom()
    if probe is None:
        return False  # No GPU detected, use CPU
    return probe["has_headroom"]


class ServerManager:
    """Manages the lifecycle of a local model server process."""

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self.model_entry: Optional[dict[str, Any]] = None
        self._running = asyncio.Event()
        self._stop_event = asyncio.Event()
        self.restart_count = 0

    @property
    def state(self) -> str:
        if self.process is None:
            return "stopped"
        if self.process.poll() is not None:
            return "exited"
        return "running"

    def is_running(self) -> bool:
        return self.state == "running"

    async def start(self, model_id: str, gpu_offload: bool = False) -> int:
        """Start the server with the specified model. Returns the port."""
        entry = find_model(model_id)
        if not entry:
            raise ValueError(f"Unknown model: {model_id}")

        self.model_entry = entry

        # Ensure model is downloaded
        if not is_model_downloaded(entry):
            raise RuntimeError(f"Model {model_id} not downloaded")

        # Find server binary
        binary = _find_server_binary()
        if not binary:
            raise RuntimeError("llama-server binary not found")

        # Select port
        self.port = select_free_port()

        # Build command. NOTE: llama-server's port flag is --port; the
        # bare -p is the PROMPT flag and would wedge startup.
        model_path = MODELS_DIR / model_id / entry["files"][0]
        cmd = [
            binary,
            "-m", str(model_path),
            "--port", str(self.port),
            "--host", "127.0.0.1",
            *entry.get("flags", []),
        ]

        if gpu_offload:
            cmd.extend(["-ngl", "99"])  # All layers to GPU
        else:
            cmd.extend(["-ngl", "0"])  # CPU only

        log.info("Starting llama-server on port %d: %s", self.port, " ".join(cmd))

        try:
            # Output goes to the OS sink, not PIPE: nothing drains the
            # pipes, and a chatty server would fill them and block.
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as e:
            raise RuntimeError(f"Failed to start llama-server: {e}") from e

        # Wait for health check to pass
        for i in range(60):
            await asyncio.sleep(HEALTH_CHECK_INTERVAL / 10)
            # Bail out early if the child already died (bad binary, bad
            # flag, port conflict) instead of waiting the full window.
            if self.process.poll() is not None:
                self.process = None
                raise RuntimeError(
                    f"llama-server exited during startup (port {self.port})"
                )
            if await health_check(self.port):
                log.info("Server healthy on port %d after %d attempts",
                         self.port, i + 1)
                return self.port

        # Startup failed: kill the child we spawned so it doesn't leak
        # as a running-but-unhealthy process holding a stale port.
        await self.stop()
        raise RuntimeError(f"Server failed to become healthy on port {self.port}")

    async def stop(self) -> None:
        """Stop the server and clean up."""
        if not self.process:
            return

        log.info("Stopping llama-server")
        try:
            self.process.terminate()
            await asyncio.wait_for(asyncio.to_thread(self.process.wait), timeout=10.0)
        except (asyncio.TimeoutError, subprocess.TimeoutExpired):
            log.warning("Server did not terminate gracefully, killing")
            self.process.kill()
            await asyncio.to_thread(self.process.wait)
        finally:
            self.process = None
            self._stop_event.set()

    async def restart_if_needed(self) -> None:
        """Check if server is healthy; restart if not."""
        if not self.is_running() or not await health_check(self.port):
            log.warning("Server unhealthy, restarting")
            self.restart_count += 1
            if self.restart_count > MAX_RESTARTS:
                # The circuit-breaker: park the manager stopped so a
                # later explicit start resets the counter instead of
                # the next call tripping the same limit instantly.
                await self.stop()
                self.restart_count = 0
                raise RuntimeError("Server failed to restart after multiple attempts")
            await self.stop()
            if self.model_entry:
                try:
                    await self.start(self.model_entry["id"])
                except Exception:
                    await self.stop()
                    raise
            else:
                await self.stop()
                raise RuntimeError("no model entry to restart")
            # A healthy restart resets the breaker.
            self.restart_count = 0

    def get_embeddings_endpoint(self) -> Optional[str]:
        """Get the embeddings endpoint URL when server is running."""
        if self.is_running() and self.port:
            return f"http://localhost:{self.port}/v1"
        return None


# ---------------------------------------------------------------------
# Configuration integration
# ---------------------------------------------------------------------

# The one live manager for this process. The embeddings client resolves
# its endpoint against THIS instance, not a fresh one - a new
# ServerManager() has no process and would always report stopped.
_manager: Optional[ServerManager] = None
_start_lock: Optional[asyncio.Lock] = None


def get_manager() -> ServerManager:
    """Return the process-wide ServerManager, creating it on first use."""
    global _manager, _start_lock
    if _manager is None:
        _manager = ServerManager()
        _start_lock = asyncio.Lock()
    return _manager


def get_start_lock() -> asyncio.Lock:
    """Lock serializing server starts (must exist after get_manager)."""
    global _start_lock
    get_manager()
    return _start_lock


def set_server_config(binary: str, model_id: str) -> None:
    """Set the server configuration (binary name and model id).

    Sends a partial update so save_config merges just the server key -
    persisting the whole load_config() view would bake env overrides
    and defaults into the user's config file.
    """
    config_mod.save_config({"server": {"binary": binary, "model": model_id}})


def get_server_config() -> tuple[str, str]:
    """Get the server configuration. Returns (binary, model_id)."""
    cfg = config_mod.load_config()
    server = cfg.get("server", {})
    return (server.get("binary", ""), server.get("model", ""))


def get_effective_embeddings_endpoint() -> Optional[str]:
    """The bundled local server's /v1 base URL while it is running and
    serving, else None. ONLY the local server - the fallback to the
    manually configured embeddings endpoint lives in the caller
    (embeddings._resolved_connection); folding it in here would make
    every configured remote endpoint look 'local' and strip its key."""
    manager = get_manager()
    if manager.is_running() and manager.port:
        return manager.get_embeddings_endpoint()
    return None


if __name__ == "__main__":
    print("Local model server manager")
    print(f"Available models: {len(MODELS)}")
    for m in MODELS:
        print(f"  {m['id']}: {m['name']} ({', '.join(m['tasks'])})")
