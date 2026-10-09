"""#360: the prompt surface carries no isolation ceremony.

ADR-0017 removed the run SOP, the branch-selector note in all its
variants, the landing ask, and the residue protocol. These tests are the
strongest guard the spec asks for: absence tests. The ceremony cannot
quietly regrow without turning one of these red.
"""

import pytest

from backend.agent.loop import _default_system_prompt
from backend.agent.subagents import AgentDef, _sub_agent_system_prompt

# Vocabulary only the removed machinery used. Generic git advice that
# predates and outlives isolation (e.g. "git worktree add" as a debugging
# tool) is deliberately absent from this list — the spec forbids git
# hygiene content, not git words.
_FORBIDDEN = [
    "run worktree",
    "run branch",
    "selected branch",
    "branch selector",
    "branch pin",
    "landing ask",
    "end-of-work landing",
    "residue protocol",
    "residue from an earlier run",
    "primary tree",
    "primary worktree",
    "land it",
    "scrap it",
    "merge-back",
    "safe-sync",
    "wip branch",
    "ADR-0010",
    "ADR-0014",
    "ADR-0015",
    "ADR-0016",
]


def _hits(text: str) -> list[str]:
    low = text.lower()
    return [w for w in _FORBIDDEN if w in low]


def test_chat_system_prompt_carries_no_isolation_ceremony():
    prompt = _default_system_prompt("/tmp/some-workspace")
    assert _hits(prompt) == []


def test_sub_agent_prompt_carries_no_isolation_ceremony():
    defn = AgentDef(
        name="probe", description="probe agent", body="Do the thing."
    )
    prompt = _sub_agent_system_prompt(defn, "/tmp/some-workspace")
    assert _hits(prompt) == []


def test_landing_ask_block_is_gone_from_the_codebase():
    import pathlib

    agent_dir = pathlib.Path(__file__).resolve().parents[1] / "agent"
    offenders = []
    for path in agent_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "End-of-work landing ask" in text or "_landing_ask" in text:
            offenders.append(path.name)
    assert offenders == []


def test_scheduler_injects_no_landing_prose():
    import pathlib

    text = (
        pathlib.Path(__file__).resolve().parents[1] / "agent" / "scheduler.py"
    ).read_text(encoding="utf-8")
    assert "# Landing" not in text
    assert "landing_mode" not in text
    assert "resolve_landing" not in text


def test_chat_system_prompt_mentions_branch_select_as_plain_switch():
    """The thin switch survives; its description must be the new one."""
    prompt = _default_system_prompt("/tmp/some-workspace")
    assert "branch_select" in prompt
    assert "branch selector" not in prompt.lower()
