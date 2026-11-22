"""Desk paths must run locally even with a legacy remote session active.

A desk path is an unnamespaced local path (e.g. ~/.yaah/desks/pool/1).
Without this guard, the legacy remote forwarding branch would forward
worker tool calls to a connected remote device, running them against a
remote interpretation of the path. The worker would operate on the wrong
host and never receive _worker_env.
"""
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_desk_workspace_skips_legacy_remote_forwarding():
    """Unnamespaced desk workspaces bypass legacy remote forwarding.

    With a connected legacy remote, a desk path (which is not namespaced)
    would normally be forwarded to the remote. But desk paths are always
    local; the worker must run locally and receive _worker_env.
    """
    # Simulate a connected legacy remote
    fake_remote = MagicMock()
    fake_remote.exec_tool = AsyncMock(return_value={"remote": True})

    # Use a real temporary directory as the desk workspace to avoid the
    # bash "directory name is invalid" error that occurs with a nonexistent path.
    with tempfile.TemporaryDirectory() as desk_dir, \
         patch("backend.agent.remote.get_remote", return_value=fake_remote), \
         patch("backend.agent.tools.is_worker_workspace", return_value=True) as mock_is_worker:
        from backend.agent.tools import execute_tool
        result = await execute_tool(
            "bash",
            {"command": "echo test"},
            desk_dir,
            conversation_id=None,
        )

        # The guard saw the desk path and skipped legacy forwarding — the
        # remote's exec_tool was never called.
        mock_is_worker.assert_called()
        fake_remote.exec_tool.assert_not_called()
        # The local bash executor ran instead
        assert "output" in result
        assert result["output"].strip() == "test"


@pytest.mark.asyncio
async def test_non_desk_workspace_still_forwards_to_legacy_remote():
    """Non-desk unnamespaced workspaces still use legacy remote forwarding.

    The fix must not break the existing behavior for regular workspaces
    that have no namespaced prefix but have a connected legacy remote.
    """
    from backend.agent.tools import execute_tool

    fake_remote = MagicMock()
    fake_remote.exec_tool = AsyncMock(return_value={"remote": True})

    # Regular workspace (not a desk path) - should still forward
    with patch("backend.agent.remote.get_remote", return_value=fake_remote):
        result = await execute_tool(
            "bash",
            {"command": "echo test"},
            "/regular/workspace",
            conversation_id=None,
        )

        # Should have been forwarded to remote
        fake_remote.exec_tool.assert_called_once()
        assert result == {"remote": True}
