"""Issue #189: sub-agent prompt text accuracy (SYN-23, SYN-24).

SYN-23: the parent-facing index description for general-purpose claimed
"all tools except ask_user and spawn_agent" while the real exclusion set
(_ALWAYS_EXCLUDED | _COMPUTER_TOOLS) is larger — the clause is now
DERIVED from those sets so it cannot drift again.

SYN-24: the final-message contract ("your final message is the only
thing the parent receives; make it self-contained: what you did, what
you changed (paths), what you verified, ...") appeared three times per
sub-agent prompt (both builtin definition bodies + a guidelines bullet)
and the copies had already drifted ("assumptions" dropped). It now has
exactly one home: the shared guidelines bullet, assumptions included.
"""
from backend.agent import subagents


def _prompt(name: str) -> str:
    defn = subagents.get_agent_def(name)
    return subagents._sub_agent_system_prompt(defn, "ws")


# ---------------------------------------------------------------- SYN-23


def test_general_purpose_description_names_every_excluded_tool():
    """The index description is derivable from the real exclusion sets."""
    desc = subagents.get_agent_def("general-purpose").description
    excluded = subagents._ALWAYS_EXCLUDED | subagents._COMPUTER_TOOLS
    for tool in excluded:
        assert tool in desc, (
            f"general-purpose description omits real exclusion {tool!r}"
        )


def test_general_purpose_description_no_false_all_tools_claim():
    desc = subagents.get_agent_def("general-purpose").description
    assert "all tools except ask_user and spawn_agent" not in desc


# ---------------------------------------------------------------- SYN-24


def test_final_message_contract_appears_exactly_once():
    for name in ("general-purpose", "explore"):
        prompt = _prompt(name)
        marker = "what you changed (paths)"
        assert prompt.count(marker) == 1, (
            f"{name}: final-message contract rendered "
            f"{prompt.count(marker)} times, expected exactly 1"
        )
        assert prompt.count("self-contained") == 1, name


def test_surviving_contract_keeps_assumptions():
    for name in ("general-purpose", "explore"):
        prompt = _prompt(name)
        assert "assumptions" in prompt, name


def test_definition_bodies_are_role_and_task_only():
    """The builtin bodies no longer carry a copy of the contract."""
    for name in ("general-purpose", "explore"):
        body = subagents.get_agent_def(name).body
        assert "final message" not in body.lower(), name
        assert "self-contained" not in body.lower(), name
