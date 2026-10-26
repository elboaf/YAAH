"""Issue #549: title call routing, token cap, cancel race."""

import asyncio
from unittest.mock import patch
import pytest

from backend.agent.loop import _generate_conversation_title
from backend.agent import model_client


@pytest.fixture(autouse=True)
def isolated_state(tmp_path):
    with patch("backend.agent.loop.load_config") as lc, \
         patch("backend.agent.loop.get_conversation") as gc, \
         patch("backend.agent.loop.get_messages") as gm, \
         patch("backend.agent.loop.update_conversation") as uc:
        lc.return_value = {"api_base": "http://localhost:9999", "api_key": "test", "model": "gpt-4"}
        gc.return_value = {"title": "New chat", "chat_type": None}
        gm.return_value = [{"role": "user", "content": "hello world"}]
        uc.return_value = None
        yield lc, gc, gm, uc


async def _make_chat_response(content):
    return {"choices": [{"message": {"content": content}}]}


@pytest.mark.asyncio
async def test_title_call_receives_model_override(isolated_state):
    called_with_model = []

    async def mock_chat(messages, tools=None, stream=False, model="", effort="", max_tokens=None):
        called_with_model.append(model)
        return await _make_chat_response("Hello World")

    with patch("backend.agent.loop.model_client.chat", side_effect=mock_chat):
        title = await _generate_conversation_title(
            conversation_id=1, user_text="hello",
            model_override="gpt-4o-mini", effort_override=None, cancel_ev=None)
    assert title == "Hello World"
    assert called_with_model[0] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_title_call_receives_effort_override(isolated_state):
    called_with_effort = []

    async def mock_chat(messages, tools=None, stream=False, model="", effort="", max_tokens=None):
        called_with_effort.append(effort)
        return await _make_chat_response("Title")

    with patch("backend.agent.loop.model_client.chat", side_effect=mock_chat):
        title = await _generate_conversation_title(
            conversation_id=1, user_text="hello",
            model_override="", effort_override="low", cancel_ev=None)
    assert title == "Title"
    assert called_with_effort[0] == "low"


@pytest.mark.asyncio
async def test_title_call_caps_max_tokens(isolated_state):
    called_with_tokens = []

    async def mock_chat(messages, tools=None, stream=False, model="", effort="", max_tokens=None):
        called_with_tokens.append(max_tokens)
        return await _make_chat_response("Title")

    with patch("backend.agent.loop.model_client.chat", side_effect=mock_chat):
        title = await _generate_conversation_title(
            conversation_id=1, user_text="hello",
            model_override="", effort_override=None, cancel_ev=None)
    assert title == "Title"
    assert called_with_tokens[0] == 32


@pytest.mark.asyncio
async def test_title_cancel_cuts_call(isolated_state):
    cancel_ev = asyncio.Event()
    chat_called = asyncio.Event()
    chat_returned = asyncio.Event()

    async def slow_chat(messages, tools=None, stream=False, model="", effort="", max_tokens=None):
        chat_called.set()
        try:
            await asyncio.wait_for(chat_returned.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        return await _make_chat_response("Title")

    async def trigger_cancel():
        await asyncio.wait_for(chat_called.wait(), timeout=5.0)
        await asyncio.sleep(0.1)
        cancel_ev.set()

    with patch("backend.agent.loop.model_client.chat", side_effect=slow_chat):
        cancel_task = asyncio.create_task(trigger_cancel())
        title = await _generate_conversation_title(
            conversation_id=1, user_text="hello",
            model_override="", effort_override=None, cancel_ev=cancel_ev)
        chat_returned.set()
        await asyncio.wait_for(cancel_task, timeout=5.0)
    assert title is None


@pytest.mark.asyncio
async def test_title_retry_also_routed_and_capped(isolated_state):
    call_count = 0
    called_with = []

    async def mock_chat(messages, tools=None, stream=False, model="", effort="", max_tokens=None):
        nonlocal call_count
        call_count += 1
        called_with.append({"model": model, "effort": effort, "max_tokens": max_tokens})
        if call_count == 1:
            return await _make_chat_response("I can help with that!")
        return await _make_chat_response("Correct Title")

    with patch("backend.agent.loop.model_client.chat", side_effect=mock_chat):
        title = await _generate_conversation_title(
            conversation_id=1, user_text="hello",
            model_override="fast-model", effort_override="low", cancel_ev=None)
    assert title == "Correct Title"
    assert call_count == 2
    assert called_with[1]["model"] == "fast-model"
    assert called_with[1]["effort"] == "low"
    assert called_with[1]["max_tokens"] == 32


@pytest.mark.asyncio
async def test_title_model_error_does_not_fail_turn(isolated_state):
    async def failing_chat(messages, tools=None, stream=False, model="", effort="", max_tokens=None):
        raise model_client.ModelError("provider error")

    with patch("backend.agent.loop.model_client.chat", side_effect=failing_chat):
        title = await _generate_conversation_title(
            conversation_id=1, user_text="hello",
            model_override="", effort_override=None, cancel_ev=None)
    assert title is None
