"""Issue #182: sub-agents get the memory tools but their system prompt
never carries the memory index the tool description promises ("The index
of saved memories is in your system prompt every turn").

Invariant: wherever a sub-agent's resolved schema set includes a memory
tool, its system prompt must carry the same persistent-memory block the
parent gets; where the block is absent (no memories yet), the invariant
is trivially satisfied by an empty index (memory.index_for_prompt returns
"").
"""
import hashlib
import json
import os

import pytest

from backend.agent import memory, subagents
from backend.agent.prompt_manifest import LOCAL_WS, LOCAL_WS_TOKEN


@pytest.fixture(autouse=True)
def _mem_root(tmp_path, monkeypatch):
    monkeypatch.setenv("YAAH_MEMORY_PATH", str(tmp_path / "mem"))
    yield


@pytest.fixture(autouse=True)
def _memory_on(tmp_path, monkeypatch):
    """Issue #169 (merged after #223 was written) made persistent memory
    opt-in with default OFF. These tests exercise the index-injection
    contract, which only holds once memory is enabled — mirror the #169
    suite's config-isolation dance and turn it on."""
    monkeypatch.setenv("YAAH_CONFIG_PATH", str(tmp_path / "config.json"))
    import importlib

    from backend.agent import config

    saved = dict(vars(config))
    importlib.reload(config)
    config.save_config({"memory": {"enabled": True}})
    yield
    monkeypatch.delenv("YAAH_CONFIG_PATH")
    for key, value in saved.items():
        setattr(config, key, value)


@pytest.fixture()
def ws(tmp_path):
    """A workspace whose memory root is the fixture root above."""
    p = tmp_path / "proj"
    p.mkdir()
    return str(p)


def _prompt(name: str, workspace: str) -> str:
    defn = subagents.get_agent_def(name)
    return subagents._sub_agent_system_prompt(defn, workspace)


def test_memory_tools_present_in_schemas():
    """Precondition: general-purpose actually resolves the memory tools —
    the half-state the issue describes (tools without the index).
    (explore's static allowlist already excludes them.)"""
    defn = subagents.get_agent_def("general-purpose")
    tools = {
        s["function"]["name"]
        for s in subagents._resolve_tools(defn, windows=True)
    }
    assert {"memory_save", "memory_read", "memory_delete"} <= tools


@pytest.mark.parametrize("name", ["general-purpose", "explore"])
def test_index_iff_memory_tools(name, ws):
    """The invariant: a sub-agent sees the memory index exactly when its
    resolved schemas include a memory tool. general-purpose resolves all
    three (and gets the block); explore's allowlist excludes them (and
    gets none)."""
    memory.save_memory(
        ws, "prefers-dark-ui", "Prefers dark UI",
        "User prefers dark themes", "user",
        "The user prefers dark UI themes.",
    )
    defn = subagents.get_agent_def(name)
    tools = {
        s["function"]["name"]
        for s in subagents._resolve_tools(defn, windows=True)
    }
    has_tools = bool(tools & {"memory_save", "memory_read", "memory_delete"})
    prompt = _prompt(name, ws)
    assert ("# Persistent memory" in prompt) == has_tools
    if has_tools:
        assert "prefers-dark-ui" in prompt


@pytest.mark.parametrize("name", ["general-purpose", "explore"])
def test_manifest_subagent_text_has_no_host_local_paths(name, ws):
    """Issue #182 return trip: the manifest harness renders sub-agent
    prompts against LOCAL_WS (a host temp dir), so the committed fixture
    must not embed that machine-specific absolute path. The rendered
    output is canonicalized to a stable token before hashing, so any
    machine regenerating the manifests produces identical bytes."""
    from backend.agent.prompt_manifest import _def_to_manifest

    memory.save_memory(
        ws, "prefers-dark-ui", "Prefers dark UI",
        "User prefers dark themes", "user",
        "The user prefers dark UI themes.",
    )
    manifest = _def_to_manifest(subagents.get_agent_def(name))
    text = manifest["text"]
    assert LOCAL_WS not in text
    # bytes/sha256 describe the canonicalized text, not the raw render:
    assert manifest["bytes"] == len(text.encode("utf-8"))
    assert manifest["sha256"] == hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()
    for section in manifest["sections"]:
        assert LOCAL_WS not in json.dumps(section)


@pytest.mark.parametrize("name", ["general-purpose", "explore"])
def test_prompt_has_no_memory_section_when_index_empty(name, ws):
    """Fresh project: the parent gets no memory block either — the
    sub-agent must not grow a fabricated one."""
    prompt = _prompt(name, ws)
    assert "# Persistent memory" not in prompt


def test_workspace_scoping_follows_the_sub_agent_workspace(tmp_path, ws):
    """The index shown is the one for the workspace the sub-agent is
    spawned into (memory is project-keyed)."""
    other = tmp_path / "other-proj"
    other.mkdir()
    memory.save_memory(
        str(other), "proj-fact", "Proj fact",
        "This project's constraint", "project",
        "A fact scoped to the other project.",
    )
    prompt = _prompt("general-purpose", str(other))
    assert "proj-fact" in prompt
    # The other workspace's index does not leak across:
    assert "prefers-dark-ui" not in prompt
