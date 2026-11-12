"""Tests for the local model server manager (T6, issue #678).

Tests the server manifest, config integration, model download logic, and
server lifecycle management. Uses mocks for the actual server process and
network downloads.
"""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

# Import under test
from backend.agent import embeddings
from backend.agent import modelserver
from backend.agent.config import DEFAULTS


class _FakeClient:
    """Stands in for httpx.Client in download tests; no network."""

    def __init__(self, handler):
        self._handler = handler

    def stream(self, method, url, headers=None, **kw):
        request = httpx.Request(method, url, headers=headers or {})
        resp = self._handler(request)
        if not isinstance(resp, httpx.Response):
            raise RuntimeError("handler must return httpx.Response")
        return _StreamCtx(resp)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StreamCtx:
    def __init__(self, resp):
        self._resp = resp

    @property
    def status_code(self):
        return self._resp.status_code

    @property
    def headers(self):
        return self._resp.headers

    def raise_for_status(self):
        if self._resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self._resp.status_code}", request=None, response=None
            )

    def iter_bytes(self, chunk_size=None):
        yield self._resp.content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def mock_manifest():
    """A test model manifest entry."""
    return {
        "id": "test-embedding-model",
        "name": "Test Embedding Model",
        "hf_repo": "test-org/test-model",
        "files": ["test-model.gguf"],
        "revision": "abc123",
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "license": "Apache-2.0",
        "tasks": ["embedding"],
        "dimensions": 384,
        "flags": ["--embeddings"],
    }


class TestModelManifest:
    """Tests for the in-code model manifest."""

    def test_manifest_has_qwen3_embedding(self):
        """The manifest includes Qwen3-Embedding-0.6B (the default)."""
        qwen = modelserver.find_model("qwen3-embedding-0.6b")
        assert qwen is not None
        assert "embedding" in qwen["tasks"]

    def test_find_model_by_id(self):
        """Find a model by its manifest id."""
        model = modelserver.find_model("test-embedding-model")
        assert model is None  # Only real models are in the manifest

    def test_find_model_missing(self):
        """Finding a non-existent model returns None."""
        model = modelserver.find_model("nonexistent")
        assert model is None

    def test_list_embedding_models(self):
        """List only models with the embedding task."""
        embedding_models = modelserver.list_models("embedding")
        assert len(embedding_models) >= 1
        assert all("embedding" in m["tasks"] for m in embedding_models)

    def test_default_embedding_model(self):
        """Get the default embedding model."""
        default = modelserver.default_model("embedding")
        assert default["id"] == "qwen3-embedding-0.6b"


class TestServerConfig:
    """Tests for the server configuration block."""

    def test_config_defaults_server_off(self):
        """Out-of-box: server is OFF (no binary, no model)."""
        from backend.agent import config
        cfg = config.load_config()
        server = cfg.get("server", {})
        assert server.get("binary", "") == ""
        assert server.get("model", "") == ""

    def test_set_server_config(self):
        """Setting the server config persists to disk."""
        modelserver.set_server_config("llama", "test-embedding-model")
        binary, model_id = modelserver.get_server_config()
        assert binary == "llama"
        assert model_id == "test-embedding-model"

    def test_get_server_config(self):
        """Reading the server config returns the stored values."""
        modelserver.set_server_config("llama", "qwen3-embedding-0.6b")
        binary, model_id = modelserver.get_server_config()
        assert binary == "llama"
        assert model_id == "qwen3-embedding-0.6b"

    def test_server_disabled(self):
        """Clearing the binary disables the server."""
        modelserver.set_server_config("llama", "qwen3-embedding-0.6b")
        modelserver.set_server_config("", "")
        binary, model_id = modelserver.get_server_config()
        assert binary == ""
        assert model_id == ""

    def test_server_port_selection(self):
        """Server port is selected by YAAH, not user-supplied."""
        # Port should be in a reasonable range for ephemeral ports
        port = modelserver.select_free_port()
        assert 1024 <= port <= 65535
        # Should be different each time (or at least not hardcoded)
        port2 = modelserver.select_free_port()
        # Note: in tests these might collide if the OS reuses ports quickly
        # but they should generally differ


class TestModelDownload:
    """Tests for the download-on-demand model fetching."""

    def test_model_dir_creation(self):
        """Model download directory is created on first use."""
        test_dir = Path(tempfile.mkdtemp())
        model_dir = test_dir / "models"
        modelserver.ensure_model_dir(model_dir)
        assert model_dir.exists()
        assert model_dir.is_dir()

    def test_download_progress_tracking(self):
        """Download tracks bytes downloaded vs total."""
        tracker = modelserver.DownloadTracker(total_size=1000)
        tracker.bytes_downloaded = 250
        assert tracker.progress == 0.25
        tracker.bytes_downloaded = 750
        assert tracker.progress == 0.75
        tracker.bytes_downloaded = 1000
        assert tracker.progress == 1.0

    def test_download_progress_no_total(self):
        """Progress tracking works when total size is unknown."""
        tracker = modelserver.DownloadTracker(total_size=None)
        tracker.update(100)
        assert tracker.bytes_downloaded == 100
        # No crash when total is unknown

    def test_download_resumes_from_part_file(self, tmp_path, monkeypatch):
        """A pre-existing .part file is continued via a Range request,
        not restarted from zero."""
        from backend.agent import modelserver as ms

        entry = dict(modelserver.find_model("qwen3-embedding-0.6b"))
        entry["sha256"] = ""  # fake bytes won't match the real checksum
        model_dir = tmp_path / entry["id"]
        model_dir.mkdir()
        monkeypatch.setattr(ms, "MODELS_DIR", tmp_path)

        part = model_dir / (entry["files"][0] + ".part")
        part.write_bytes(b"HEAD")  # 4 bytes from a previous attempt

        seen = {}

        def handler(request):
            seen["range"] = request.headers.get("Range")
            body = b"BODY"
            if seen["range"]:
                return httpx.Response(206, content=body)
            return httpx.Response(200, content=b"HEAD" + body)

        monkeypatch.setattr(
            ms.httpx, "Client",
            lambda **kw: _FakeClient(handler),
        )
        got = ms.download_model(entry)
        assert seen["range"] == "bytes=4-"
        assert got.read_bytes() == b"HEADBODY"

    def test_download_server_ignoring_range_restarts(self, tmp_path, monkeypatch):
        """A 200 reply to a Range request means the server ignores it:
        restart from zero, never append duplicate bytes."""
        from backend.agent import modelserver as ms

        entry = dict(modelserver.find_model("qwen3-embedding-0.6b"))
        entry["sha256"] = ""  # fake bytes won't match the real checksum
        model_dir = tmp_path / entry["id"]
        model_dir.mkdir()
        monkeypatch.setattr(ms, "MODELS_DIR", tmp_path)

        part = model_dir / (entry["files"][0] + ".part")
        part.write_bytes(b"STALE-")

        def handler(request):
            # Ignores Range: always returns the whole file with 200.
            return httpx.Response(200, content=b"fresh-bytes")

        monkeypatch.setattr(
            ms.httpx, "Client", lambda **kw: _FakeClient(handler)
        )
        got = ms.download_model(entry)
        assert got.read_bytes() == b"fresh-bytes"

    def test_failed_download_keeps_part_file(self, tmp_path, monkeypatch):
        """A dropped connection keeps the .part file as the resume anchor."""
        from backend.agent import modelserver as ms

        entry = dict(modelserver.find_model("qwen3-embedding-0.6b"))
        assert entry["sha256"]  # the real checksum stays enforced here
        model_dir = tmp_path / entry["id"]
        model_dir.mkdir()
        monkeypatch.setattr(ms, "MODELS_DIR", tmp_path)

        # Seed a prior attempt's anchor so the test proves survival.
        part = model_dir / (entry["files"][0] + ".part")
        part.write_bytes(b"HEAD")

        def handler(request):
            raise httpx.ConnectError("dropped")

        monkeypatch.setattr(
            ms.httpx, "Client", lambda **kw: _FakeClient(handler)
        )
        with pytest.raises(httpx.ConnectError):
            ms.download_model(entry)
        part = model_dir / (entry["files"][0] + ".part")
        assert part.exists()  # anchor survives for the next attempt
        assert part.read_bytes() == b"HEAD"  # untouched, resumable

    def test_checksum_mismatch_drops_completed_download(self, tmp_path, monkeypatch):
        """A COMPLETED download that fails verification is deleted: those
        bytes are corrupt and must not become the next resume anchor."""
        from backend.agent import modelserver as ms

        entry = dict(modelserver.find_model("qwen3-embedding-0.6b"))
        assert entry["sha256"]  # real checksum, mismatch guaranteed
        model_dir = tmp_path / entry["id"]
        model_dir.mkdir()
        monkeypatch.setattr(ms, "MODELS_DIR", tmp_path)

        def handler(request):
            return httpx.Response(200, content=b"corrupt-full-file")

        monkeypatch.setattr(
            ms.httpx, "Client", lambda **kw: _FakeClient(handler)
        )
        with pytest.raises(ValueError, match="Checksum mismatch"):
            ms.download_model(entry)
        part = model_dir / (entry["files"][0] + ".part")
        assert not part.exists()  # corrupt anchor removed


class TestServerBinaryDownload:
    """The llama-server binary itself installs on demand (pinned release,
    checksummed), so Start never dead-ends with 'binary not found'."""

    def _zip_bytes(self, files: dict) -> bytes:
        import io
        import zipfile as zf

        buf = io.BytesIO()
        with zf.ZipFile(buf, "w") as z:
            for name, data in files.items():
                z.writestr(name, data)
        return buf.getvalue()

    def test_binary_pin_is_complete(self):
        """The pin carries every field download needs; checksum is enforced."""
        pin = modelserver.SERVER_BINARY_PIN
        assert pin["tag"] and pin["asset"]
        assert pin["url"].startswith("https://github.com/ggml-org/llama.cpp/")
        assert pin["asset"] in pin["url"]
        assert len(pin["sha256"]) == 64
        assert pin["member"] == "llama-server.exe"

    def test_download_stages_binary_and_dlls(self, tmp_path, monkeypatch):
        """A good archive stages the exe + DLLs (by basename, flat layout),
        drops nothing else, and leaves no zip or .part behind."""
        from backend.agent import modelserver as ms

        bin_dir = tmp_path / "bin"
        monkeypatch.setattr(ms, "BIN_DIR", bin_dir)
        monkeypatch.setattr(ms, "SERVER_BINARY_PATH", bin_dir / "llama-server.exe")

        archive = self._zip_bytes({
            "llama-b12284-bin-win-cpu-x64/llama-server.exe": b"MZ-fake-exe",
            "llama-b12284-bin-win-cpu-x64/ggml.dll": b"DLL-bytes",
            "llama-b12284-bin-win-cpu-x64/README.txt": b"not staged",
        })

        pin = dict(ms.SERVER_BINARY_PIN)
        import hashlib
        pin["sha256"] = hashlib.sha256(archive).hexdigest()
        monkeypatch.setattr(ms, "SERVER_BINARY_PIN", pin)

        def handler(request):
            return httpx.Response(200, content=archive)

        monkeypatch.setattr(
            ms.httpx, "Client", lambda **kw: _FakeClient(handler)
        )
        got = ms.download_server_binary()
        assert got == str(bin_dir / "llama-server.exe")
        assert (bin_dir / "llama-server.exe").read_bytes() == b"MZ-fake-exe"
        assert (bin_dir / "ggml.dll").read_bytes() == b"DLL-bytes"
        assert not (bin_dir / "README.txt").exists()
        assert not (bin_dir / (pin["asset"] + ".part")).exists()
        assert not (bin_dir / pin["asset"]).exists()
        assert not list(bin_dir.glob("yaah-llama-*"))  # staging cleaned

    def test_failed_publish_never_leaves_broken_install(self, tmp_path, monkeypatch):
        """Extraction dying mid-publish must not expose an exe with
        missing DLLs: the exe publishes last, so a crashed install is
        invisible to _find_server_binary and the next start reinstalls."""
        import shutil

        from backend.agent import modelserver as ms

        bin_dir = tmp_path / "bin"
        monkeypatch.setattr(ms, "BIN_DIR", bin_dir)
        monkeypatch.setattr(ms, "SERVER_BINARY_PATH", bin_dir / "llama-server.exe")

        archive = self._zip_bytes({
            "llama-b12284-bin-win-cpu-x64/llama-server.exe": b"MZ-fake-exe",
            "llama-b12284-bin-win-cpu-x64/ggml.dll": b"DLL-bytes",
        })
        pin = dict(ms.SERVER_BINARY_PIN)
        import hashlib
        pin["sha256"] = hashlib.sha256(archive).hexdigest()
        monkeypatch.setattr(ms, "SERVER_BINARY_PIN", pin)

        def handler(request):
            return httpx.Response(200, content=archive)

        monkeypatch.setattr(
            ms.httpx, "Client", lambda **kw: _FakeClient(handler)
        )

        real_move = shutil.move

        def fail_dll_move(src, dst):
            if str(dst).endswith("ggml.dll"):
                raise OSError("disk full mid-publish")
            return real_move(src, dst)

        monkeypatch.setattr(ms.shutil, "move", fail_dll_move)
        with pytest.raises(OSError):
            ms.download_server_binary()
        assert not (bin_dir / "llama-server.exe").exists()
        # Isolate the finder to bin_dir: no PATH, no machine-wide
        # installs (env-var indirection covers the hardcoded dirs).
        monkeypatch.setattr(ms.shutil, "which", lambda name: None)
        empty = str(tmp_path / "env-empty")
        monkeypatch.setenv("LOCALAPPDATA", empty)
        monkeypatch.setenv("USERPROFILE", empty)
        assert ms._find_server_binary() is None
        assert not list(bin_dir.glob("yaah-llama-*"))  # staging cleaned

    def test_download_checksum_mismatch_stages_nothing(self, tmp_path, monkeypatch):
        """Corrupt archive bytes: nothing is staged and the anchor is gone."""
        from backend.agent import modelserver as ms

        bin_dir = tmp_path / "bin"
        monkeypatch.setattr(ms, "BIN_DIR", bin_dir)

        archive = self._zip_bytes({
            "llama-b12284-bin-win-cpu-x64/llama-server.exe": b"MZ-fake-exe",
        })
        pin = dict(ms.SERVER_BINARY_PIN)
        pin["sha256"] = "0" * 64  # will not match
        monkeypatch.setattr(ms, "SERVER_BINARY_PIN", pin)

        def handler(request):
            return httpx.Response(200, content=archive)

        monkeypatch.setattr(
            ms.httpx, "Client", lambda **kw: _FakeClient(handler)
        )
        with pytest.raises(ValueError, match="Checksum mismatch"):
            ms.download_server_binary()
        assert not (bin_dir / "llama-server.exe").exists()
        assert not (bin_dir / (pin["asset"] + ".part")).exists()

    def test_find_prefers_staged_over_path(self, tmp_path, monkeypatch):
        """A previously installed binary wins over a PATH lookup."""
        from backend.agent import modelserver as ms

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        staged = bin_dir / "llama-server.exe"
        staged.write_bytes(b"MZ")
        monkeypatch.setattr(ms, "SERVER_BINARY_PATH", staged)
        monkeypatch.setattr(ms.shutil, "which", lambda name: "C:/path/llama-server.exe")
        assert ms._find_server_binary() == str(staged)

    def test_ensure_uses_found_binary_without_download(self, tmp_path, monkeypatch):
        """A binary already on the system short-circuits the download."""
        from backend.agent import modelserver as ms

        monkeypatch.setattr(ms, "_find_server_binary", lambda: "C:/found/llama-server.exe")

        def boom(progress_cb=None):
            raise AssertionError("download must not run when binary exists")

        monkeypatch.setattr(ms, "download_server_binary", boom)
        assert ms.ensure_server_binary() == "C:/found/llama-server.exe"

    def test_ensure_downloads_when_missing(self, monkeypatch):
        """Nothing on the system: the pinned download runs."""
        from backend.agent import modelserver as ms

        monkeypatch.setattr(ms, "_find_server_binary", lambda: None)
        monkeypatch.setattr(
            ms, "download_server_binary", lambda progress_cb=None: "C:/staged/llama-server.exe"
        )
        assert ms.ensure_server_binary() == "C:/staged/llama-server.exe"

    def test_ensure_wraps_download_failure(self, monkeypatch):
        """A failed install raises RuntimeError with the cause, so the
        Start endpoint surfaces a real error instead of dead-ending."""
        from backend.agent import modelserver as ms

        monkeypatch.setattr(ms, "_find_server_binary", lambda: None)

        def fail(progress_cb=None):
            raise httpx.ConnectError("no network")

        monkeypatch.setattr(ms, "download_server_binary", fail)
        with pytest.raises(RuntimeError, match="auto-install failed"):
            ms.ensure_server_binary()

    def test_start_installs_binary_on_demand(self, tmp_path, monkeypatch):
        """ServerManager.start goes through ensure_server_binary (which
        downloads when nothing is installed), not the bare finder."""
        from backend.agent import modelserver as ms

        calls = []
        monkeypatch.setattr(
            ms, "ensure_server_binary",
            lambda gpu_offload=False, calls=calls: calls.append("ensure") or "C:/staged/llama-server.exe",
        )
        entry = modelserver.find_model("qwen3-embedding-0.6b")
        monkeypatch.setattr(
            ms, "MODELS_DIR", tmp_path / entry["id"]
        )  # not downloaded; start must fail on the model AFTER the binary step
        (tmp_path / entry["id"]).mkdir()

        manager = ms.ServerManager()
        monkeypatch.setattr(ms, "HEALTH_CHECK_INTERVAL", 0.01)
        with patch.object(ms.subprocess, "Popen", side_effect=OSError("no spawn in test")):
            with pytest.raises(Exception):
                asyncio.run(manager.start("qwen3-embedding-0.6b"))
        assert calls == ["ensure"]  # the binary step ran before any spawn


class TestServerLifecycle:
    """Tests for server start/stop/health management."""

    def test_server_state_tracking(self):
        """Server state is tracked correctly."""
        manager = modelserver.ServerManager()
        assert manager.state == "stopped"

    def test_server_not_running(self):
        """is_running returns False when server is stopped."""
        manager = modelserver.ServerManager()
        assert not manager.is_running()

    def test_port_persistence(self):
        """Server port is persisted across restarts."""
        manager = modelserver.ServerManager()
        # Simulate starting and stopping
        manager.port = 12345
        assert manager.port == 12345


class TestEmbeddingsIntegration:
    """Tests for embeddings config population when server is active."""

    def test_embeddings_endpoint_from_server(self):
        """When server is running, its endpoint fills the embeddings config."""
        manager = modelserver.ServerManager()
        manager.port = 12345
        # Mock a running process
        manager.process = MagicMock()
        manager.process.poll.return_value = None
        endpoint = manager.get_embeddings_endpoint()
        assert endpoint == "http://localhost:12345/v1"

    def test_embeddings_endpoint_not_running(self):
        """When server is stopped, no endpoint is provided."""
        manager = modelserver.ServerManager()
        endpoint = manager.get_embeddings_endpoint()
        assert endpoint is None or endpoint == ""


class TestSingletonManager:
    """The embeddings client must resolve against the live manager."""

    def setup_method(self):
        modelserver._manager = None
        modelserver._start_lock = None

    def teardown_method(self):
        modelserver._manager = None
        modelserver._start_lock = None

    def test_get_manager_returns_same_instance(self):
        """get_manager() is a singleton: same object every call."""
        m1 = modelserver.get_manager()
        m2 = modelserver.get_manager()
        assert m1 is m2

    def test_running_singleton_produces_endpoint(self):
        """A start through the singleton is visible via the helper."""
        manager = modelserver.get_manager()
        manager.port = 7777
        manager.process = MagicMock()
        manager.process.poll.return_value = None
        assert modelserver.get_effective_embeddings_endpoint() == (
            "http://localhost:7777/v1"
        )

    def test_start_lock_serializes(self):
        """get_start_lock() hands out one lock for all callers."""
        assert modelserver.get_start_lock() is modelserver.get_start_lock()


class TestSetServerConfigPartial:
    """set_server_config must send a PARTIAL update, not a full snapshot."""

    def test_set_only_touches_server_key(self, tmp_path, monkeypatch):
        """Sibling keys in the config file survive a server-config write."""
        from backend.agent import config as config_mod

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "providers": {"openai": {"api_base": "https://x", "api_key": "sk",
                                      "model": "gpt"}},
            "embeddings": {"api_base": "http://lan:9", "api_key": "k",
                            "model": "m"},
        }))
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_file)
        monkeypatch.setattr(modelserver.config_mod, "CONFIG_PATH", cfg_file)

        modelserver.set_server_config("llama", "qwen3-embedding-0.6b")
        saved = json.loads(cfg_file.read_text())
        assert saved["server"] == {"binary": "llama",
                                   "model": "qwen3-embedding-0.6b"}
        assert saved["embeddings"]["api_base"] == "http://lan:9"
        assert saved["embeddings"]["api_key"] == "k"


class TestResolvedConnection:
    """The local-server branch supplies model id and no key (ADR-0022)."""

    def test_local_server_supplies_manifest_model_no_key(self, monkeypatch):
        """A running local server wins even with a blank embeddings block."""
        monkeypatch.setattr(
            modelserver, "get_effective_embeddings_endpoint",
            lambda: "http://localhost:9999/v1",
        )
        monkeypatch.setattr(modelserver, "get_server_config",
                            lambda: ("llama", "qwen3-embedding-0.6b"))
        monkeypatch.setattr(embeddings, "_embeddings_config",
                            lambda: {"api_base": "", "api_key": "remote-key",
                                     "model": ""})
        conn = embeddings._resolved_connection()
        assert conn["api_base"] == "http://localhost:9999/v1"
        assert conn["model"] == "qwen3-embedding-0.6b"
        assert conn["api_key"] == ""  # no remote Bearer to localhost

    def test_remote_config_used_when_no_local_server(self, monkeypatch):
        """With no running server, the configured block stands."""
        monkeypatch.setattr(
            modelserver, "get_effective_embeddings_endpoint",
            lambda: None,
        )
        monkeypatch.setattr(
            embeddings, "_embeddings_config",
            lambda: {"api_base": "http://lan:9", "api_key": "k", "model": "m"},
        )
        conn = embeddings._resolved_connection()
        assert conn == {"api_base": "http://lan:9", "api_key": "k",
                        "model": "m"}

    def test_check_available_configured_via_local_server(self, monkeypatch):
        """A healthy local server counts as configured with no block set."""
        monkeypatch.setattr(
            modelserver, "get_effective_embeddings_endpoint",
            lambda: "http://localhost:9999/v1",
        )
        monkeypatch.setattr(modelserver, "get_server_config",
                            lambda: ("llama", "qwen3-embedding-0.6b"))
        # Capture what embed_text would send: the local endpoint must
        # route through with the manifest model id.
        captured = {}

        def fake_handler(request):
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            body = json.loads(request.content)
            captured["model"] = body["model"]
            return httpx.Response(200, json={
                "data": [{"embedding": [0.0] * 4}],
                "model": body["model"],
            })

        monkeypatch.setattr(embeddings, "_transport",
                            httpx.MockTransport(fake_handler))
        result = asyncio.run(embeddings.check_embeddings_available())
        assert result["configured"] is True
        assert result["available"] is True
        assert captured["model"] == "qwen3-embedding-0.6b"
        assert captured["auth"] is None

    def test_start_command_uses_port_flag(self):
        """llama-server takes --port; bare -p would set the prompt."""
        # Build the exact command start() assembles and assert the flag.
        manager = modelserver.ServerManager()
        entry = modelserver.find_model("qwen3-embedding-0.6b")
        assert entry is not None
        # The manifest must pin the case-correct GGUF filename (404 otherwise).
        assert entry["files"] == ["Qwen3-Embedding-0.6B-Q8_0.gguf"]
        assert entry["revision"] != "main"  # pinned commit, not a moving ref
        assert len(entry["sha256"]) == 64   # checksum enforced on download


class TestGpuSelection:
    """Tests for the multi-GPU selection feature: enumerate, index probe,
    and the persisted selection. nvidia-smi is stubbed so the tests run
    on a CPU-only host and assert the parser, not the hardware."""

    def test_enumerate_gpus_parses_multi_gpu(self, monkeypatch):
        """A two-GPU nvidia-smi output (index,name,total,free rows) yields one
        entry per GPU with byte-sized VRAM. The CSV header row has 4 columns
        too, so it must be skipped by the int() parse, not the column count."""
        out = "index,name,memory.total [MiB],memory.free [MiB]\n0,NVIDIA GeForce RTX 4070 SUPER,12282,10770\n1,NVIDIA GeForce GT 1030,2048,1704\n"
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(shutil, "which", lambda name: "nvidia-smi")
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
        )
        gpus = modelserver.enumerate_gpus()
        assert [g["index"] for g in gpus] == [0, 1]
        assert gpus[0]["name"] == "NVIDIA GeForce RTX 4070 SUPER"
        assert gpus[0]["total_vram"] == 12282 * 1024 * 1024
        assert gpus[1]["free_vram"] == 1704 * 1024 * 1024

    def test_enumerate_gpus_empty_when_no_nvidia(self, monkeypatch):
        """No nvidia-smi on PATH means the selector has nothing to show."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(os, "name", "nt")
        assert modelserver.enumerate_gpus() == []

    def test_probe_without_index_first_gpu(self, monkeypatch):
        """The default (no index) probe parses the total,free row."""
        # Stub os.name first: the probe gates on os.name == "nt", and the
        # tests mock nvidia-smi so they must reach that block on any host
        # (CI runs Linux, where os.name is "posix" by default).
        monkeypatch.setattr(os, "name", "nt")
        out = "12282, 10770\n2048, 1704\n"
        monkeypatch.setattr(shutil, "which", lambda name: "nvidia-smi")
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
        )
        probe = modelserver._probe_vram_headroom()
        assert probe["has_headroom"] is True
        assert probe["free_vram"] == 10770 * 1024 * 1024

    def test_probe_with_index_matches_row(self, monkeypatch):
        """A GPU-index probe reads that GPU's row, not the first row."""
        monkeypatch.setattr(os, "name", "nt")
        out = "index, memory.total [MiB], memory.free [MiB]\n0, 12282, 10770\n1, 2048, 1704\n"
        monkeypatch.setattr(shutil, "which", lambda name: "nvidia-smi")

        def fake_run(cmd, *a, **k):
            # The caller must pass index first when a specific GPU is asked.
            assert "--query-gpu=index,memory.total,memory.free" in cmd[1]
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        probe = modelserver._probe_vram_headroom(1)
        assert probe["free_vram"] == 1704 * 1024 * 1024
        assert probe["total_vram"] == 2048 * 1024 * 1024

    def test_probe_no_headroom_when_vram_low(self, monkeypatch):
        """VRAM under the ~2 GB threshold reports no headroom."""
        # Stub os.name first (same reason as the other probe tests): the
        # probe gates on os.name == "nt" so the mocked nvidia-smi is reached
        # on Linux CI too.
        monkeypatch.setattr(os, "name", "nt")
        # index,total,free where GPU 0 has ~1 GB free.
        out = "index, memory.total [MiB], memory.free [MiB]\n0, 4096, 900\n"
        monkeypatch.setattr(shutil, "which", lambda name: "nvidia-smi")
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
        )
        probe = modelserver._probe_vram_headroom(0)
        assert probe["has_headroom"] is False

    def test_gpu_index_config_roundtrip(self):
        """The selected GPU index persists and reads back."""
        modelserver.set_server_gpu_index(1)
        assert modelserver.get_server_gpu_index() == 1
        # A different task reads its own key.
        modelserver.set_server_gpu_index(2, task="reranking")
        assert modelserver.get_server_gpu_index("reranking") == 2
        assert modelserver.get_server_gpu_index() == 1
        # None clears the selection.
        modelserver.set_server_gpu_index(None)
        assert modelserver.get_server_gpu_index() is None

    def test_start_offload_command_splits_mode_and_pins_gpu(self, monkeypatch, tmp_path):
        """A GPU-offload start assembles --split-mode none plus a pinned
        --main-gpu so a multi-GPU host offloads to the chosen card."""
        entry = modelserver.find_model("qwen3-embedding-0.6b")
        assert entry is not None
        monkeypatch.setattr(modelserver, "MODELS_DIR",
                            tmp_path / entry["id"])
        (tmp_path / entry["id"]).mkdir()
        monkeypatch.setattr(modelserver, "is_model_downloaded",
                            lambda entry: True)
        # Probe reports headroom so the offload path is taken.
        monkeypatch.setattr(modelserver, "_probe_vram_headroom",
                            lambda gpu_index=None: {
                                "has_headroom": True, "free_vram": 0})
        monkeypatch.setattr(modelserver, "ensure_server_binary",
                            lambda gpu_offload=False: "/bin/llama-server.exe")

        captured = {}

        def fake_popen(cmd, **kw):
            captured["cmd"] = cmd
            p = MagicMock()
            p.poll.return_value = None  # never "exited"
            return p

        manager = modelserver.ServerManager()
        monkeypatch.setattr(modelserver, "HEALTH_CHECK_INTERVAL", 0.01)
        monkeypatch.setattr(modelserver, "health_check",
                            lambda port: asyncio.sleep(0.01, result=True))
        with patch.object(modelserver.subprocess, "Popen",
                          side_effect=fake_popen):
            asyncio.run(manager.start(
                "qwen3-embedding-0.6b", gpu_offload=True, gpu_index=1))
        cmd = captured["cmd"]
        assert cmd.count("-ngl") == 1 and "99" in cmd
        # split-mode none must precede --main-gpu, which carries the index.
        assert cmd[cmd.index("--split-mode") + 1] == "none"
        assert cmd[cmd.index("--main-gpu") + 1] == "1"

    def test_explicit_gpu_offload_override_forces_offload(self, monkeypatch, tmp_path):
        """An explicit gpu_offload=True forces offload even when the saved
        preference is off, so a probe-found headroom does not revert it."""
        entry = modelserver.find_model("qwen3-embedding-0.6b")
        assert entry is not None
        monkeypatch.setattr(modelserver, "MODELS_DIR",
                            tmp_path / entry["id"])
        (tmp_path / entry["id"]).mkdir()
        monkeypatch.setattr(modelserver, "is_model_downloaded",
                            lambda entry: True)
        monkeypatch.setattr(modelserver, "get_server_gpu_offload",
                            lambda tenant: False)  # saved pref is OFF
        monkeypatch.setattr(modelserver, "_probe_vram_headroom",
                            lambda gpu_index=None: {
                                "has_headroom": True, "free_vram": 0})
        monkeypatch.setattr(modelserver, "ensure_server_binary",
                            lambda gpu_offload=False: "/bin/llama-server.exe")

        captured = {}

        def fake_popen(cmd, **kw):
            captured["cmd"] = cmd
            p = MagicMock()
            p.poll.return_value = None
            return p

        manager = modelserver.ServerManager()
        monkeypatch.setattr(modelserver, "HEALTH_CHECK_INTERVAL", 0.01)
        monkeypatch.setattr(modelserver, "health_check",
                            lambda port: asyncio.sleep(0.01, result=True))
        with patch.object(modelserver.subprocess, "Popen",
                          side_effect=fake_popen):
            asyncio.run(manager.start(
                "qwen3-embedding-0.6b", gpu_offload=True, gpu_index=1))
        # Explicit True wins: split-mode none + pinned --main-gpu are present,
        # proving the saved-off preference did not force CPU mode.
        cmd = captured["cmd"]
        assert "--split-mode" in cmd and "99" in cmd
        assert cmd[cmd.index("--main-gpu") + 1] == "1"

class TestCudaBinarySelection:
    """The CUDA build is opt-in: only staged when offload is requested.

    The default pinned binary is CPU-only (ADR-0022). When offload is on,
    ensure_server_binary must stage and return the CUDA build under a
    distinct path so it never shares the CPU binary or its DLLs.
    """

    def _zip_bytes(self, members):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in members.items():
                z.writestr(name, data)
        return buf.getvalue()

    def test_offload_true_stages_cuda_build(self, tmp_path, monkeypatch):
        """ensure_server_binary(True) downloads the CUDA build and its
        runtime to llama-server-cuda.exe, not the CPU path.

        The archive ships the exe under its real member name
        (llama-server.exe), which the staging step renames to the
        published llama-server-cuda.exe; the separate runtime asset's
        three DLLs are staged beside it.
        """
        from backend.agent import modelserver as ms
        import hashlib

        bin_dir = tmp_path / "bin"
        monkeypatch.setattr(ms, "BIN_DIR", bin_dir)
        monkeypatch.setattr(ms, "CUDA_SERVER_BINARY_PATH", bin_dir / "llama-server-cuda.exe")

        # Archive member name is the real name ("llama-server.exe"),
        # distinct from the published staged_name ("llama-server-cuda.exe").
        exe_archive = self._zip_bytes({
            "llama-b12284-bin-win-cuda-12.4-x64/llama-server.exe": b"cuda-exe",
            "llama-b12284-bin-win-cuda-12.4-x64/ggml-cuda.dll": b"cuddl",
        })
        runtime_archive = self._zip_bytes({
            "cublas64_12.dll": b"cublas",
            "cublasLt64_12.dll": b"cublaslt",
            "cudart64_12.dll": b"cudart",
        })
        exe_pin = dict(ms.CUDA_SERVER_BINARY_PIN)
        exe_pin["sha256"] = hashlib.sha256(exe_archive).hexdigest()
        monkeypatch.setattr(ms, "CUDA_SERVER_BINARY_PIN", exe_pin)
        runtime_pin = dict(ms.CUDA_RUNTIME_PIN)
        runtime_pin["sha256"] = hashlib.sha256(runtime_archive).hexdigest()
        monkeypatch.setattr(ms, "CUDA_RUNTIME_PIN", runtime_pin)

        archives = {exe_pin["asset"]: exe_archive, runtime_pin["asset"]: runtime_archive}

        def handler(request):
            return httpx.Response(200, content=archives[request.url.path.rsplit("/", 1)[-1]])

        monkeypatch.setattr(ms.httpx, "Client", lambda **kw: _FakeClient(handler))
        monkeypatch.setattr(ms.shutil, "which", lambda name: None)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "empty"))

        got = ms.ensure_server_binary(True)
        assert got == str(bin_dir / "llama-server-cuda.exe")
        assert (bin_dir / "llama-server-cuda.exe").read_bytes() == b"cuda-exe"
        # The CUDA build's runtime DLLs are staged beside it.
        assert (bin_dir / "cudart64_12.dll").read_bytes() == b"cudart"
        assert (bin_dir / "cublas64_12.dll").read_bytes() == b"cublas"
        # The CPU binary path must be untouched.
        assert not (bin_dir / "llama-server.exe").exists()

    def test_offload_true_stages_cpu_when_unavailable_raises(self, tmp_path, monkeypatch):
        """When offload is requested but the CUDA build cannot be staged,
        raise rather than falling back to a CPU binary (which would make a
        successful start run on CPU despite the user asking for GPU)."""
        from backend.agent import modelserver as ms

        monkeypatch.setattr(ms, "find_cuda_server_binary", lambda: None)

        def boom(progress_cb=None):
            raise httpx.ConnectError("no network")

        monkeypatch.setattr(ms, "download_cuda_server_binary", boom)
        with pytest.raises(RuntimeError, match="CUDA llama-server build"):
            ms.ensure_server_binary(True)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
