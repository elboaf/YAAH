"""#195: the two "Invoked skills" wrapper copies had drifted.

The queued-path wrapper (_apply_injected_skills) omitted the composer
phrasing and the never-deny clause that the turn-path wrapper carried.
One helper must now serve both call sites, and its text is pinned here.
"""
import pytest

from backend.agent import loop, skills as skill_registry


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setattr(skill_registry, "SKILLS_DIR", d)
    monkeypatch.setattr(skill_registry, "_scanned", False)
    dd = d / "ask-matt"
    dd.mkdir()
    (dd / "SKILL.md").write_text(
        "---\nname: ask-matt\ndescription: router\n---\n\nROUTER-BODY-MARKER\n",
        encoding="utf-8",
    )
    return d


CONTRACT_PHRASES = (
    "# Invoked skills",
    "a chip or /name in the composer",
    "authoritative",
    "manual-invocation skills are deliberately hidden",
    "Never tell the user an invoked skill is unavailable",
)


def test_shared_wrapper_carries_full_contract():
    """The single wrapper copy must carry the complete contract text (#195)."""
    text = loop.invoked_skills_wrapper("BODY-MARKER")
    for phrase in CONTRACT_PHRASES:
        assert phrase in text, f"wrapper contract missing: {phrase!r}"
    assert text.endswith("BODY-MARKER")


@pytest.mark.usefixtures("skills_dir")
def test_queued_path_uses_shared_wrapper():
    """_apply_injected_skills must emit the full contract, including the
    never-deny clause and composer phrasing the queued variant dropped."""
    skill_registry.ensure_scanned()
    messages = [{"role": "system", "content": "base"}]
    loop._apply_injected_skills({"skills": ["ask-matt"]}, [], messages)
    sys = messages[0]["content"]
    for phrase in CONTRACT_PHRASES:
        assert phrase in sys, f"queued-path wrapper contract missing: {phrase!r}"
    assert "ROUTER-BODY-MARKER" in sys
