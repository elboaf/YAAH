"""Sub-agent provider routing (handoff: keep the local KV cache hot).

The global ``subagent_provider`` setting (and per-agent ``provider``
frontmatter) routes a sub-agent's model calls at a different provider than
the parent conversation's — so spawns stop competing for a single-slot
local server's slot and evicting its prompt cache. Only ROUTING changes:
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
    assert _resolve_subagent_model(AgentDef(name="x", description="", body="")) == ("", "")


def test_global_setting_routes_provider_model(cfg):
    cfg(subagent_provider="lfm25")
    model, err = _resolve_subagent_model(AgentDef(name="x", description="", body=""))
    assert err == ""
    # A bare provider name is qualified with that provider's model — chat's
    # bare-id branch would otherwise keep the AMBIENT (parent's) provider.
    assert model == "lfm25::lfm-8b"


def test_global_setting_provider_model_pins_model(cfg):
    cfg(subagent_provider="lfm25::other-model")
    model, err = _resolve_subagent_model(None)
    assert err == ""
    assert model == "lfm25::other-model"


def test_agentdef_provider_beats_global(cfg):
    cfg(subagent_provider="lfm25")
    d = AgentDef(name="x", description="", body="", provider="main::big")
    assert _resolve_subagent_model(d) == ("main::big", "")


def test_agentdef_provider_none_falls_through_to_global(cfg):
    cfg(subagent_provider="lfm25")
    model, _ = _resolve_subagent_model(AgentDef(name="x", description="", body=""))
    assert model == "lfm25::lfm-8b"


def test_unknown_provider_is_a_structured_error(cfg):
    cfg(subagent_provider="nope")
    model, err = _resolve_subagent_model(None)
    assert model == ""
    assert "Unknown provider 'nope'" in err


def test_unknown_agentdef_provider_beats_global_and_errors(cfg):
    cfg(subagent_provider="lfm25")
    d = AgentDef(name="x", description="", body="", provider="ghost")
    _, err = _resolve_subagent_model(d)
    assert "Unknown provider 'ghost'" in err


# ------------------------------------------------------------ runtime


@pytest.mark.asyncio
async def test_routing_reaches_chat_main_loop(cfg, capture, tmp_path):
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
async def test_agentdef_provider_used_at_runtime(cfg, capture, tmp_path):
    cfg(subagent_provider="lfm25")
    d = AgentDef(name="pinned", description="", body="", provider="main::big")
    await subagents.run_sub_agent(d, "look", str(tmp_path))
    assert capture.models == ["main::big"]


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


# ------------------------------------------------------------ frontmatter


def test_frontmatter_provider_parsed(tmp_path, monkeypatch):
    (tmp_path / "routed.md").write_text(
        "---\n"
        "name: routed\n"
        "description: pinned routing.\n"
        "provider: lfm25::lfm-8b\n"
        "---\n"
        "body",
        encoding="utf-8",
    )
    monkeypatch.setattr(subagents, "AGENTS_DIR", tmp_path)
    subagents.scan_agents()
    d = subagents.get_agent_def("routed")
    assert d is not None
    assert d.provider == "lfm25::lfm-8b"


def test_frontmatter_provider_blank_is_none(tmp_path, monkeypatch):
    (tmp_path / "plain.md").write_text(
        "---\nname: plain\ndescription: d.\nprovider: \n---\nbody",
        encoding="utf-8",
    )
    monkeypatch.setattr(subagents, "AGENTS_DIR", tmp_path)
    subagents.scan_agents()
    d = subagents.get_agent_def("plain")
    assert d is not None
    assert d.provider is None
