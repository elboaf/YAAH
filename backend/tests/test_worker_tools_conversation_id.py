"""Worker tools must receive conversation identity through the main loop.

Reproduces #303-shape bug: without forwarding conversation_id for
WORKER_WORLD_TOOLS in the loop, submit_contribution fails with
"submit_contribution runs only from a worker chat" and spawn_worker
spawns without a parent.
"""
import pytest
from backend.agent.tools import WORKER_WORLD_TOOLS
from backend.agent.loop import _execute_with_progress


@pytest.mark.asyncio
async def test_worker_tools_receive_conversation_id():
    """The main loop forwards conversation identity for all worker tools.

    Verifies the fix for the missing forward that made submit_contribution
    always fail and spawn_worker spawn unparented chats.
    """
    # Verify WORKER_WORLD_TOOLS is non-empty (schema is registered).
    assert len(WORKER_WORLD_TOOLS) >= 3, "worker tools must be registered"
    assert "submit_contribution" in WORKER_WORLD_TOOLS
    assert "spawn_worker" in WORKER_WORLD_TOOLS
    assert "review_contribution" in WORKER_WORLD_TOOLS

    # Inspect the loop's _execute_with_progress tool_kwargs construction
    # by checking it sets conversation_id for a worker tool name.
    # We do this by mocking execute_tool and verifying it receives the kwarg.
    from unittest.mock import AsyncMock, patch
    import json

    box = {}
    tool_name = "submit_contribution"
    args = {"workspace": None, "branch": "test", "base_commit": "abc",
            "summary": "test", "desk_dir": "/tmp/desk"}

    mock_exec = AsyncMock()
    mock_exec.return_value = {"status": "ok"}

    with patch("backend.agent.loop.execute_tool", mock_exec):
        async for _ in _execute_with_progress(
            tool_name, args, "/tmp/ws", "call-1", box,
            conversation_id=42,
        ):
            pass

    # Verify execute_tool was called with conversation_id
    call_kwargs = mock_exec.call_args.kwargs
    assert call_kwargs.get("conversation_id") == 42, (
        "execute_tool must receive conversation_id for worker tools; "
        "without it submit_contribution always fails"
    )


@pytest.mark.asyncio
async def test_spawn_worker_receives_conversation_id():
    """spawn_worker must get conversation identity for parent linking."""
    from unittest.mock import AsyncMock, patch

    box = {}
    args = {"workspace": None, "message": "test task"}

    mock_exec = AsyncMock()
    mock_exec.return_value = {"conversation_id": 99, "desk_dir": "/tmp/desk"}

    with patch("backend.agent.loop.execute_tool", mock_exec):
        async for _ in _execute_with_progress(
            "spawn_worker", args, "/tmp/ws", "call-2", box,
            conversation_id=55,
        ):
            pass

    call_kwargs = mock_exec.call_args.kwargs
    assert call_kwargs.get("conversation_id") == 55, (
        "spawn_worker must receive conversation_id so it can set "
        "the spawned chat's worker_parent_id and wake it"
    )
