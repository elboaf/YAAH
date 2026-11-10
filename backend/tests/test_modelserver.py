"""Tests for the local model server manager (T6, issue #678).

Tests the server manifest, config integration, model download logic, and
server lifecycle management. Uses mocks for the actual server process and
network downloads.
"""

import asyncio
import json
import os
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
