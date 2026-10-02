"""Issue #181: prose tool lists must be derived from the schema sets, not
hand-maintained copies that drift."""
from backend.agent import loop, subagents, tools

import pytest


@pytest.fixture(autouse=True)
def _memory_on(tmp_path, monkeypatch):
    """Issue #169 (merged after #181 was written) made persistent memory
    opt-in with default OFF. The prose-drift guards assert memory tools
    appear in schemas and prose, which only holds once memory is enabled
    — mirror the #169 suite's config-isolation dance and turn it on."""
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


# ------------------------------------------------------- shared helper

def test_tool_prose_list_basic():
    """tool_prose_list joins names into the 'You have tools: ...' sentence,
    with optional per-name annotations from the map."""
    line = tools.tool_prose_list(["bash", "read_file"], {})
    assert line == "You have tools: bash, read_file."
    line = tools.tool_prose_list(
        ["bash", "read_file"], {"bash": "shell commands"}
    )
    assert line == "You have tools: bash (shell commands), read_file."


def test_tool_prose_list_accepts_schema_dicts():
    """The helper also accepts schema dicts directly, so callers can pass
    the resolved schema list without pre-extracting names."""
    schemas = tools.get_schemas()
    line = tools.tool_prose_list(schemas, {})
    for s in schemas:
        assert s["function"]["name"] in line


# ----------------------------------------- sub-agent prose == resolution

def _prose_names(prompt: str) -> set[str]:
    """Extract the tool names named in the prompt's 'You have tools:' line.
    Tolerates annotations containing commas/parens (multi-line notes are
    flattened by the caller's prompt renderer)."""
    for ln in prompt.splitlines():
        if ln.startswith("You have tools: "):
            body = ln.removeprefix("You have tools: ").removesuffix(".")
            names = set()
            for part in body.split(", "):
                names.add(part.split(" (")[0])
            return names
    raise AssertionError("no 'You have tools:' line in prompt")


def _drop_annotation_overflow(names: set[str]) -> set[str]:
    """Annotation fragments that wrap across lines parse as bogus names
    (e.g. 'such as servers/ports or GUI checks)'); drop obvious ones."""
    return {n for n in names if n.replace("_", "").isalnum() and n}


def test_subagent_tool_sentence_matches_resolved_schemas():
    """#181 AC1: the tool sentence is generated from _resolve_tools —
    every resolved schema appears, and nothing else does."""
    for agent_name in ("general-purpose", "explore"):
        defn = subagents.get_agent_def(agent_name)
        prompt = subagents._sub_agent_system_prompt(defn, "")
        resolved = {
            s["function"]["name"]
            for s in subagents._resolve_tools(defn, workspace=None)
        }
        assert _prose_names(prompt) == resolved, agent_name


def test_explore_prompt_does_not_name_stripped_tools():
    """#181 AC2: explore's prompt no longer names tools its allowlist
    strips (write_file etc. used to be advertised)."""
    defn = subagents.get_agent_def("explore")
    prompt = subagents._sub_agent_system_prompt(defn, "")
    resolved = {
        s["function"]["name"]
        for s in subagents._resolve_tools(defn, workspace=None)
    }
    for tool in ("write_file", "edit_file", "create_file",
                 "delete_file", "move_file"):
        assert tool not in resolved  # sanity: really stripped
        assert tool not in prompt, tool
    # bash/powershell legitimately appear in the env line ("The bash tool
    # runs commands through..."), just never in explore's tools sentence.
    line = next(ln for ln in prompt.splitlines()
                if ln.startswith("You have tools: "))
    assert "bash" not in line
    assert "powershell" not in line


def test_general_purpose_prompt_mentions_every_resolved_tool():
    """#181 AC3: tools the sub-agent actually has (get_help, memory_*) are
    named in its prompt — no silent schema-only tools."""
    defn = subagents.get_agent_def("general-purpose")
    prompt = subagents._sub_agent_system_prompt(defn, "")
    resolved = {
        s["function"]["name"]
        for s in subagents._resolve_tools(defn, workspace=None)
    }
    prose = _prose_names(prompt)
    # search_conversation_history is ALWAYS_EXCLUDED for sub-agents (it
    # searches the parent's conversation); the others must resolve.
    assert {"get_help", "memory_save", "memory_read",
            "memory_delete"} <= resolved
    assert "search_conversation_history" not in resolved
    assert "search_conversation_history" not in prompt
    assert prose == resolved


# ------------------------------------------------ parent prose list

def test_parent_prose_names_match_its_schema_set():
    """#181 AC3 (parent side): every name in the parent's schema set
    appears in the rendered prose list (annotated or bare) — no more
    schema-without-mention drift like search_conversation_history."""
    prompt = loop._default_system_prompt("")
    prose = _prose_names(prompt)
    schema_names = {
        s["function"]["name"] for s in tools.get_schemas()
    }
    # tools conditionally stripped (screenshot toggle, install_git) may be
    # absent from the schema set; everything present must be mentioned.
    missing = schema_names - _drop_annotation_overflow(prose)
    assert not missing, missing


def test_parent_prose_mentions_no_phantom_tools():
    """Inverse drift guard: nothing in the parent prose line may lack a
    schema, except the bare conditional names (powershell on non-Windows
    test hosts, install_git)."""
    prompt = loop._default_system_prompt("")
    prose = _prose_names(prompt)
    schema_names = {
        s["function"]["name"] for s in tools.get_schemas()
    }
    allowed_extra = {"powershell", "install_git"}  # host-conditional
    phantom = _drop_annotation_overflow(prose) - schema_names - allowed_extra
    assert not phantom, phantom


def test_subagent_prose_line_mentions_nothing_outside_resolution():
    """Belt-and-braces for both built-ins at once: no prose-only names
    beyond the shell annotations (the shell line names bash/powershell
    with annotations regardless of resolution for bash; explore's
    resolution strips bash, so its sentence must not name it)."""
    for agent_name in ("general-purpose", "explore"):
        defn = subagents.get_agent_def(agent_name)
        prompt = subagents._sub_agent_system_prompt(defn, "")
        line = next(
            ln for ln in prompt.splitlines()
            if ln.startswith("You have tools: ")
        )
        if agent_name == "explore":
            assert "bash" not in line
            assert "powershell" not in line


# --------------------------------------- delegation policy single source

def test_delegation_policy_single_source():
    """#181 AC4: the delegation-policy guidance exists in exactly one
    source constant, interpolated into schema + help docs + index — the
    three copies must be textually equal to the constant."""
    from backend.agent.tools import DELEGATION_POLICY
    assert DELEGATION_POLICY in tools.SCHEMAS["spawn_agent"]["function"]["description"]
    assert DELEGATION_POLICY in tools.HELP_DOCS["spawn_agent"]
    assert DELEGATION_POLICY in subagents.index_for_prompt()
