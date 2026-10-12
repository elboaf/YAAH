"""Sub-agent model scope (#383): where spawn_agent children run.

Resolution: the spawning chat's pin (per-chat sub-agent dropdown, complete
"provider::model") > the global ``subagent_provider`` setting > the parent
conversation's own model. The per-agent ``provider:`` frontmatter layer was
removed with #383 (no user agent files ever used it). Only ROUTING changes:
prompt construction stays exactly as-is (owner's explicit non-goal).
"""
import asyncio

import pytest

from backend.agent import subagents
from backend.agent.subagents import AgentDef, _resolve_subagent_model


@pytest.fixture
def cfg(monkeypatch):
    """A config with two providers, patched where subagents reads it."""
    from backend.agent import config as config_mod

    def _set(**kw):
        base = {
            "providers": {
                "main": {"api_base": "http://main.local", "api_key": "", "model": "big"},
                "lfm25": {"api_base": "http://lfm.local:8090", "api_key": "", "model": "lfm-8b"},
            },
            "active_provider": "main",
            "subagent_provider": "",
        }
        base.update(kw)
        monkeypatch.setattr(subagents, "load_config", lambda: dict(base))
        return base

    return _set


class ModelCapture:
    """chat stub recording the model kwarg of every call."""

    def __init__(self):
        self.models: list[str] = []
        self.calls = 0

    async def __call__(self, messages, tools=None, stream=True, model=""):
        self.calls += 1
        self.models.append(model)

        async def it():
            yield {"type": "content", "text": "done"}
            yield {"type": "finish", "reason": "stop"}

        return it()


@pytest.fixture
def capture(monkeypatch):
    cap = ModelCapture()
    monkeypatch.setattr(subagents.model_client, "chat", cap)
    return cap


# ------------------------------------------------------------ resolver


def test_default_inherits_parent_provider(cfg):
    cfg()
    assert _resolve_subagent_model() == ("", "")


def test_chat_pin_wins_over_global(cfg):
    cfg(subagent_provider="lfm25")
    model, err = _resolve_subagent_model("main::big")
    assert err == ""
    assert model == "main::big"


def test_empty_pin_falls_through_to_global(cfg):
    cfg(subagent_provider="lfm25")
    model, err = _resolve_subagent_model("")
    assert err == ""
    # A bare provider name is qualified with that provider's model — chat's
    # bare-id branch would otherwise keep the AMBIENT (parent's) provider.
    assert model == "lfm25::lfm-8b"


def test_global_setting_routes_provider_model(cfg):
    cfg(subagent_provider="lfm25")
    model, err = _resolve_subagent_model(None)
    assert err == ""
    assert model == "lfm25::lfm-8b"


def test_global_setting_provider_model_pins_model(cfg):
    cfg(subagent_provider="lfm25::other-model")
    model, err = _resolve_subagent_model(None)
    assert err == ""
    assert model == "lfm25::other-model"


def test_unknown_provider_is_a_structured_error(cfg):
    cfg(subagent_provider="nope")
    model, err = _resolve_subagent_model(None)
    assert model == ""
    assert "Unknown provider 'nope'" in err


def test_unknown_provider_in_chat_pin_errors(cfg):
    cfg(subagent_provider="lfm25")
    _, err = _resolve_subagent_model("ghost::big")
    assert "Unknown provider 'ghost'" in err


# ------------------------------------------------------------ runtime


@pytest.mark.asyncio
async def test_chat_pin_reaches_chat_main_loop(cfg, capture, tmp_path):
    cfg(subagent_provider="lfm25")
    result = await subagents.run_sub_agent(
        subagents.get_agent_def("explore"), "look", str(tmp_path),
        chat_scope="main::big",
    )
    assert result["status"] == "completed"
    assert capture.models == ["main::big"]


@pytest.mark.asyncio
async def test_global_routing_reaches_chat_main_loop(cfg, capture, tmp_path):
    cfg(subagent_provider="lfm25")
    result = await subagents.run_sub_agent(
        subagents.get_agent_def("explore"), "look", str(tmp_path)
    )
    assert result["status"] == "completed"
    assert capture.models == ["lfm25::lfm-8b"]


@pytest.mark.asyncio
async def test_no_routing_passes_no_model_kwarg(cfg, capture, tmp_path):
    cfg()
    await subagents.run_sub_agent(
        subagents.get_agent_def("explore"), "look", str(tmp_path)
    )
    assert capture.models == [""]


@pytest.mark.asyncio
async def test_unknown_provider_errors_spawn_without_model_call(cfg, capture, tmp_path):
    cfg(subagent_provider="ghost")
    result = await subagents.run_sub_agent(
        subagents.get_agent_def("explore"), "look", str(tmp_path)
    )
    assert result["status"] == "error"
    assert "Unknown provider 'ghost'" in result["error"]
    assert capture.calls == 0


@pytest.mark.asyncio
async def test_unknown_provider_in_chat_pin_errors_spawn(cfg, capture, tmp_path):
    cfg()
    result = await subagents.run_sub_agent(
        subagents.get_agent_def("explore"), "look", str(tmp_path),
        chat_scope="ghost::big",
    )
    assert result["status"] == "error"
    assert "Unknown provider 'ghost'" in result["error"]
    assert capture.calls == 0


@pytest.mark.asyncio
async def test_spawn_batch_threads_chat_scope(cfg, capture, tmp_path):
    """#383: the chat's pin hands down to every sub-agent of the batch."""
    cfg(subagent_provider="lfm25")
    results = await subagents.spawn_batch(
        [
            {"call_id": "c1", "agent_type": "explore", "prompt": "one"},
            {"call_id": "c2", "agent_type": "general-purpose", "prompt": "two"},
        ],
        str(tmp_path),
        asyncio.Event(),
        chat_scope="main::big",
    )
    assert results["c1"]["status"] == "completed"
    assert results["c2"]["status"] == "completed"
    assert capture.models == ["main::big", "main::big"]


@pytest.mark.asyncio
async def test_spawn_batch_isolates_routing_error(cfg, capture, tmp_path):
    """A bad routing name fails that call with a structured error; the
    parent's turn (other calls in the batch) survives."""
    cfg(subagent_provider="ghost")
    results = await subagents.spawn_batch(
        [
            {"call_id": "c1", "agent_type": "explore", "prompt": "one"},
            {"call_id": "c2", "agent_type": "no-such-agent", "prompt": "two"},
        ],
        str(tmp_path),
        asyncio.Event(),
    )
    assert results["c1"]["status"] == "error"
    assert "Unknown provider 'ghost'" in results["c1"]["error"]
    assert "Unknown agent_type" in results["c2"]["error"]
    assert capture.calls == 0
