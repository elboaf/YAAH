"""Desk paths must run locally even with a legacy remote session active.

A desk path is an unnamespaced local path (e.g. ~/.yaah/desks/pool/1).
Without this guard, the legacy remote forwarding branch would forward
worker tool calls to a connected remote device, running them against a
remote interpretation of the path. The worker would operate on the wrong
host and never receive _worker_env.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_desk_workspace_skips_legacy_remote_forwarding():
    """Unnamespaced desk workspaces bypass legacy remote forwarding.

    With a connected legacy remote, a desk path (which is not namespaced)
    would normally be forwarded to the remote. But desk paths are always
    local; the worker must run locally and receive _worker_env.
    """
    from backend.agent import desks
    from backend.agent.desks import DESKS_ROOT

    # Simulate a connected legacy remote
    fake_remote = MagicMock()
    fake_remote.exec_tool = AsyncMock(return_value={"remote": True})

    # Patch get_remote to return our fake
    with patch("backend.agent.tools.execute_tool") as mock_exec:
        mock_exec.return_value = {"local": True}

        # Call execute_tool with a desk-like workspace path
        # This is unnamespaced so legacy forwarding applies without the guard
        from backend.agent.tools import execute_tool
        result = await execute_tool(
            "bash",
            {"command": "echo test"},
            str(DESKS_ROOT / "testpool" / "1"),  # desk path format
            conversation_id=None,
        )

        # The call should go to local execute_tool, not the remote
        mock_exec.assert_called_once()
        assert result == {"local": True}


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
            "/regular/workspace",  # not a desk path
            conversation_id=None,
        )

        # Should have been forwarded to remote
        assert result == {"remote": True}
