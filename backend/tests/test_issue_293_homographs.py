"""#293: homograph disambiguation in the spoken-briefing ruleset.

Kokoro picks one pronunciation per spelling and sentence context doesn't
help it, but the writing model knows exactly which sense it means. The
rule therefore lives in the say ruleset (prompt layer only — no
speak.py/pipeline changes): inside the <say> tag, spelling IS
pronunciation, so tense/context-dependent homographs go phonetic.
Sibling to #294 (symbol/locator stripping); extends the landed #206
speech-writing ruleset.
"""
import pytest

from backend.agent import loop
from backend.tests.support_say import (  # noqa: F401 (restored_config is a fixture)
    restored_config,
    save_config_with_voice as _config_with_voice,
    say_section as _say_section,
)


# ---- rule present ------------------------------------------------------------


def test_homograph_rule_is_in_the_say_ruleset(restored_config):
    """The acceptance criterion: the spoken-briefing block gains the
    phonetic-spelling rule with concrete before/after examples."""
    prompt = loop._default_system_prompt("")
    section = _say_section(prompt)
    assert "Homographs go phonetic" in section
    # 2-3 concrete before/after examples, in the issue's spirit:
    assert '"read" \u2192 "red"' in section
    assert '"lead" \u2192 "led"' in section
    assert '"bass" \u2192 "base"' in section


def test_rule_carries_the_stripped_tag_rationale(restored_config):
    """The rule explains WHY phonetic spelling is safe: the tag is
    spoken, never rendered, so mis-spelled words cost nothing in chat."""
    section = _say_section(loop._default_system_prompt(""))
    assert "never shown" in section or "never rendered" in section


# ---- scope -------------------------------------------------------------------


def test_rule_lands_inside_say_section_only(restored_config):
    """The rule must not leak past the say section: everything before
    the section is byte-identical to a prompt without it, and the
    ask-user section carries no homograph bytes."""
    prompt = loop._default_system_prompt("")
    say_opening = "Spoken briefing (voice read-aloud):\n"
    head = prompt.split(say_opening, 1)[0]
    tail = prompt.split("\nInterview the user", 1)
    assert len(tail) == 2, "say-section sentinel moved; fix the slicing"
    assert "Homographs go phonetic" not in head
    assert "Homographs go phonetic" not in tail[1]


# ---- emissions gate -----------------------------------------------------------


def test_rule_absent_when_say_emissions_disabled(restored_config):
    """#207 gate: with voice.say_emissions off, the whole say section is
    not generated — the homograph rule must not appear either."""
    _config_with_voice(say_emissions=False)
    prompt = loop._default_system_prompt("")
    assert "Spoken briefing (voice read-aloud):" not in prompt
    assert "Homographs go phonetic" not in prompt
