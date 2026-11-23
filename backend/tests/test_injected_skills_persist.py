"""Test that steered/queued skill invocations persist to the conversation
row (same as load_skill tool path) so the prefix cache survives the
next turn. This specifically covers the _apply_injected_skills path,
which #392 did not cover.

Issue #810: steered skill invocation re-prefills the whole conversation
after the head because the injected block is this-turn-only (inserted at
slot 1, never persisted). Fix: merge onto persisted_skill_tail and persist
to the conversation row, identical to the load_skill tool path.
"""
import pytest
from unittest.mock import AsyncMock, patch

from backend.agent.loop import _apply_injected_skills, invoked_skills_wrapper
from backend.db.database import (
    create_conversation, delete_conversation, get_conversation,
    update_conversation,
)


@pytest.mark.asyncio
async def test_injected_skills_persist_to_row():
    """Steered skill invocation in static mode persists to the conversation row."""
    cid = await create_conversation("test-injected-persist")
    try:
        item = {"skills": ["impeccable"], "text": "steered message"}
        loaded_skills = []
        messages = [{"role": "system", "content": "head"}]

        with patch("backend.agent.loop.skill_registry.split_known_unknown") as mock_split, \
             patch("backend.agent.loop.skill_registry.bodies_for_prompt") as mock_bodies, \
             patch("backend.agent.loop.skill_registry.skill_tail_index") as mock_slot:
            mock_split.return_value = (["impeccable"], [])
            mock_bodies.return_value = "# Impeccable skill body"
            mock_slot.return_value = 1

            events = await _apply_injected_skills(
                item, cid, loaded_skills, messages,
                static_mode=True, workspace="/test/workspace"
            )

            # Verify: no skill_not_found events
            assert events == []

            # Verify: loaded_skills updated
            assert "impeccable" in loaded_skills

            # Verify: persisted to the row
            row = await get_conversation(cid)
            assert row is not None
            assert "# Impeccable skill body" in row["persisted_skill_tail"]
            assert row["persisted_skill_tail_ws"] == "/test/workspace"

            # Verify: skill block inserted at tail slot (index 1 after head)
            assert len(messages) == 2
            assert messages[1]["role"] == "user"
            assert "Impeccable skill body" in messages[1]["content"]
    finally:
        await delete_conversation(cid)


@pytest.mark.asyncio
async def test_injected_skills_merge_with_existing_tail():
    """Multiple steered skill invocations merge onto the tail, don't overwrite."""
    cid = await create_conversation("test-injected-merge")
    try:
        # First invocation
        item1 = {"skills": ["impeccable"], "text": "steer 1"}
        loaded_skills = []
        messages = [{"role": "system", "content": "head"}]

        with patch("backend.agent.loop.skill_registry.split_known_unknown") as mock_split, \
             patch("backend.agent.loop.skill_registry.bodies_for_prompt") as mock_bodies, \
             patch("backend.agent.loop.skill_registry.skill_tail_index") as mock_slot:
            mock_split.return_value = (["impeccable"], [])
            mock_bodies.return_value = "# Impeccable skill body"
            mock_slot.return_value = 1

            await _apply_injected_skills(
                item1, cid, loaded_skills, messages,
                static_mode=True, workspace="/test/workspace"
            )

            # Second invocation
            item2 = {"skills": ["ponytail"], "text": "steer 2"}
            loaded_skills = []
            messages2 = [{"role": "system", "content": "head"}]

            mock_split.return_value = (["ponytail"], [])
            mock_bodies.return_value = "# Ponytail skill body"
            mock_slot.return_value = 1

            await _apply_injected_skills(
                item2, cid, loaded_skills, messages2,
                static_mode=True, workspace="/test/workspace"
            )

            # Verify: merged with separator
            row = await get_conversation(cid)
            tail = row["persisted_skill_tail"]
            assert "# Impeccable skill body" in tail
            assert "# Ponytail skill body" in tail
            assert "Impeccable" in tail and "Ponytail" in tail
    finally:
        await delete_conversation(cid)


@pytest.mark.asyncio
async def test_injected_skills_workspace_mismatch_replaces():
    """A skill loaded in a different workspace REPLACES the stale tail."""
    cid = await create_conversation("test-injected-ws")
    try:
        # First: load in one workspace
        item1 = {"skills": ["impeccable"], "text": "steer 1"}
        loaded_skills = []
        messages = [{"role": "system", "content": "head"}]

        with patch("backend.agent.loop.skill_registry.split_known_unknown") as mock_split, \
             patch("backend.agent.loop.skill_registry.bodies_for_prompt") as mock_bodies, \
             patch("backend.agent.loop.skill_registry.skill_tail_index") as mock_slot:
            mock_split.return_value = (["impeccable"], [])
            mock_bodies.return_value = "# Impeccable skill body"
            mock_slot.return_value = 1

            await _apply_injected_skills(
                item1, cid, loaded_skills, messages,
                static_mode=True, workspace="/workspace/A"
            )

            # Second: load in different workspace
            item2 = {"skills": ["ponytail"], "text": "steer 2"}
            loaded_skills = []
            messages2 = [{"role": "system", "content": "head"}]

            mock_split.return_value = (["ponytail"], [])
            mock_bodies.return_value = "# Ponytail skill body"
            mock_slot.return_value = 1

            await _apply_injected_skills(
                item2, cid, loaded_skills, messages2,
                static_mode=True, workspace="/workspace/B"
            )

            # Verify: replaced, not merged (different workspace)
            row = await get_conversation(cid)
            assert "# Ponytail skill body" in row["persisted_skill_tail"]
            assert "# Impeccable skill body" not in row["persisted_skill_tail"]
            assert row["persisted_skill_tail_ws"] == "/workspace/B"
    finally:
        await delete_conversation(cid)


@pytest.mark.asyncio
async def test_injected_skills_non_static_mode():
    """Non-static mode still appends to system prompt (unchanged behavior)."""
    cid = await create_conversation("test-injected-nonstatic")
    try:
        item = {"skills": ["impeccable"], "text": "steer"}
        loaded_skills = []
        messages = [{"role": "system", "content": "head"}]

        with patch("backend.agent.loop.skill_registry.split_known_unknown") as mock_split, \
             patch("backend.agent.loop.skill_registry.bodies_for_prompt") as mock_bodies:
            mock_split.return_value = (["impeccable"], [])
            mock_bodies.return_value = "# Impeccable skill body"

            await _apply_injected_skills(
                item, cid, loaded_skills, messages,
                static_mode=False, workspace=""
            )

            # Verify: appended to system prompt
            assert "Impeccable skill body" in messages[0]["content"]
            assert len(messages) == 1  # No new message inserted

            # Verify: NOT persisted to the row
            row = await get_conversation(cid)
            assert row["persisted_skill_tail"] is None or row["persisted_skill_tail"] == ""
    finally:
        await delete_conversation(cid)
