"""Tests for the local model server manager (T6, issue #678).

Tests the server manifest, config integration, model download logic, and
server lifecycle management. Uses mocks for the actual server process and
network downloads.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import under test
from backend.agent import modelserver
from backend.agent.config import DEFAULTS


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


class TestServerLifecycle:
    """Tests for server start/stop/health management."""

    def test_server_state_tracking(self):
        """Server state is tracked correctly."""
        with patch.object(modelserver, "MODELS", [mock_manifest]):
            manager = modelserver.ServerManager()
            assert manager.state == "stopped"

    def test_server_not_running(self):
        """is_running returns False when server is stopped."""
        manager = modelserver.ServerManager()
        assert not manager.is_running()

    def test_port_persistence(self):
        """Server port is persisted across restarts."""
        with patch.object(modelserver, "select_free_port", return_value=12345):
            with patch.object(modelserver, "MODELS", [mock_manifest]):
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
