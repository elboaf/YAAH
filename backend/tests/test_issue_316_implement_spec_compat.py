"""Issue #316: /implement-spec compatibility decisions (ADR-0012).

Pins, as tests, what the study-and-decide issue concluded:

- Outcome (b) chosen: convention-only support. /implement-spec stays
  unbundled; ADR-0012 records the decision and the nested-worktree
  convention for running the skill by hand.
- The vendored `resolving-merge-conflicts` skill stays (deletion NOT
  accepted) and carries a landing-contract compatibility note.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ADR_DIR = REPO / "docs" / "adr"
ADR_0012 = ADR_DIR / "0012-implement-spec-compatibility.md"
CONFLICT_SKILL = (
    REPO / "backend" / "bundled_skills" / "resolving-merge-conflicts" / "SKILL.md"
)


def _text(path: Path) -> str:
    assert path.exists(), f"missing: {path}"
    return path.read_text(encoding="utf-8")


def test_adr_0012_exists_and_records_convention_only_outcome():
    text = _text(ADR_0012)
    assert "implement-spec" in text
    assert re.search(r"\(b\)|convention-only", text)
    # One outcome chosen and justified: an Accepted status, like every ADR.
    assert re.search(r"Status:.*Accepted", text)


def test_adr_0012_decides_nested_worktree_story():
    """Convention: implementer trees under the chat dir, child-first
    removal, sweeper-visible by path arithmetic."""
    text = _text(ADR_0012)
    assert ".scratch/chat-" in text
    assert "child-first" in text
    assert "wt_sweep" in text


def test_adr_0012_carries_landing_contract_pin_line():
    """The pin line: wherever implement-spec-style flows are documented,
    landing targets the chat's selected branch, never master."""
    text = _text(ADR_0012)
    assert re.search(r"[Ll]anding-contract pin", text)
    # master is excluded from landing targets: a chat whose selector
    # points at master must stop until a non-master branch is selected.
    assert "selected non-master branch" in text
    assert "selected branch" in text
    assert "master never moves" in text


def test_resolving_merge_conflicts_is_annotated_not_deleted():
    """Upstream deleted the skill as a harness concern; YAAH IS the
    harness, so the vendored copy stays with a compat note."""
    text = _text(CONFLICT_SKILL)
    assert "ADR-0010" in text
    # The note reconciles the skill's never-abort rule with the run
    # SOP's free `merge --abort` inside a chat worktree.
    assert "--abort" in text
