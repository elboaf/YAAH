"""Persistent memory (backend/agent/memory.py): per-workspace file
layout, the save/read/delete tool executors, index maintenance, and
prompt injection."""
import os

import pytest

from backend.agent import memory


@pytest.fixture(autouse=True)
def _mem_root(tmp_path, monkeypatch):
    monkeypatch.setenv("YAAH_MEMORY_PATH", str(tmp_path / "mem"))
    yield


WS = str(os.getcwd())  # any stable absolute path; no FS access needed


# ---- project key ------------------------------------------------------------

def test_project_key_is_stable_and_path_derived():
    a = memory.project_key(WS)
    assert a == memory.project_key(WS)
    assert len(a) == 16
    assert a != memory.project_key(str(WS + "-other"))


def test_project_key_case_insensitive_on_windows(monkeypatch):
    monkeypatch.setattr(memory.os, "name", "nt")
    upper = memory.project_key("C:\\Proj")
    lower = memory.project_key("c:\\proj")
    assert upper == lower


def test_project_key_remote_namespaced_is_raw_string():
    local = memory.project_key("C:\\somewhere\\remote:h1:\\proj")
    remote = memory.project_key("remote:h1:\\proj")
    assert remote != local
    assert memory.project_key("remote:h1:\\proj") == memory.project_key(
        "remote:h1:\\proj"
    )


def test_project_key_default_workspace_uses_home():
    # "" and "." are the same Default pseudo-workspace -> home dir
    assert memory.project_key("") == memory.project_key(".")
    assert memory.project_key("") != memory.project_key("C:\\nowhere")


# ---- save / read / delete round-trip ----------------------------------------

def test_save_creates_file_and_index_line():
    r = memory.save_memory(WS, "prefers dark ui", "Prefers dark UI",
                           "User prefers dark themes", "user",
                           "The user prefers dark UI themes.")
    assert "error" not in r
    d = memory.memory_dir(WS)
    assert (d / "prefers-dark-ui.md").exists()
    idx = (d / "MEMORY.md").read_text(encoding="utf-8")
    assert "[Prefers dark UI](prefers-dark-ui.md) — User prefers dark themes" in idx


def test_read_returns_full_content():
    memory.save_memory(WS, "fact", "Fact", "A fact", "project", "body text here")
    r = memory.read_memory(WS, "fact")
    assert "body text here" in r["content"]
    assert r["name"] == "fact"


def test_update_replaces_index_line_instead_of_appending():
    memory.save_memory(WS, "fact", "Fact", "v1", "project", "v1")
    memory.save_memory(WS, "fact", "Fact", "v2", "project", "v2")
    idx = (memory.memory_dir(WS) / "MEMORY.md").read_text("utf-8")
    assert idx.count("](fact.md)") == 1
    assert "v2" in idx and "v1" not in idx


def test_delete_removes_file_and_index_line():
    memory.save_memory(WS, "gone", "Gone", "soon", "project", "x")
    memory.save_memory(WS, "kept", "Kept", "stays", "project", "y")
    assert "error" not in memory.delete_memory(WS, "gone")
    assert not (memory.memory_dir(WS) / "gone.md").exists()
    idx = (memory.memory_dir(WS) / "MEMORY.md").read_text("utf-8")
    assert "gone.md" not in idx and "kept.md" in idx
    assert "error" in memory.delete_memory(WS, "gone")  # double delete


def test_invalid_names_rejected():
    for bad in ("../escape", "a/b", "", "UPPER CASE!", "x" * 200, ".hidden"):
        assert "error" in memory.save_memory(WS, bad, "t", "d", "project", "c")
        assert "error" in memory.read_memory(WS, bad)
        assert "error" in memory.delete_memory(WS, bad)


def test_unknown_read_and_delete_error_cleanly():
    assert "error" in memory.read_memory(WS, "nope")
    assert "error" in memory.delete_memory(WS, "nope")


def test_type_falls_back_to_project_and_empty_content_rejected():
    r = memory.save_memory(WS, "t1", "T", "d", "bogus-type", "c")
    assert r["type"] == "project"
    assert "error" in memory.save_memory(WS, "t2", "T", "d", "user", "   ")


# ---- prompt injection -------------------------------------------------------

def test_index_for_prompt_empty_until_first_memory():
    assert memory.index_for_prompt(WS) == ""
    memory.save_memory(WS, "fact", "Fact", "A fact", "project", "detail")
    block = memory.index_for_prompt(WS)
    assert "Persistent memory" in block
    assert "fact.md" in block
    assert "memory_save" in block  # tool guidance included


def test_index_for_prompt_template_seeded_dir_is_silent():
    memory.ensure_dir(WS)
    assert memory.index_for_prompt(WS) == ""


def test_index_for_prompt_caps_long_index():
    for i in range(400):
        memory.save_memory(WS, f"m-{i}", f"M{i}", "x" * 200, "project", "c")
    block = memory.index_for_prompt(WS)
    assert len(block) < memory.MAX_INDEX_CHARS + 2000
    assert "…[truncated]" in block


def test_index_for_prompt_never_raises_on_corrupt_index(tmp_path):
    d = memory.ensure_dir(WS)
    (d / "MEMORY.md").write_bytes(b"\xff\xfe\x00broken")
    assert isinstance(memory.index_for_prompt(WS), str)


# ---- tool dispatch wiring ----------------------------------------------------

@pytest.mark.asyncio
async def test_executors_route_through_execute_tool():
    from backend.agent.tools import execute_tool, tool_risk

    assert tool_risk("memory_read") == "read"
    assert tool_risk("memory_save") == "mutating"
    assert tool_risk("memory_delete") == "mutating"

    r = await execute_tool("memory_save",
                           {"name": "wire", "title": "Wire", "description": "d",
                            "type": "project", "content": "c"}, WS)
    assert "error" not in r
    r = await execute_tool("memory_read", {"name": "wire"}, WS)
    assert "error" not in r
    r = await execute_tool("memory_delete", {"name": "wire"}, WS)
    assert "error" not in r


def test_schemas_registered_platform_neutral():
    from backend.agent.tools import SCHEMAS

    for n in ("memory_save", "memory_read", "memory_delete"):
        assert n in SCHEMAS
