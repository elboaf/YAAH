"""Issue #295: narrator personas for the spoken <say> briefings.

voice.say_persona is a fixed v1 enum (neutral|jester|elitest) that appends
a persona block INSIDE the gated spoken-briefing section of the system
prompt. The contract under test:

- neutral (and absent/unset/unknown) produces byte-identical prompts to
  the pre-feature baseline — no regression for existing users;
- jester/elitest change ONLY the spoken-briefing section — the persona
  bytes sit between the section opening and the ask-user section, and the
  never-in-chat boundary is written into the persona text itself;
- with say_emissions off, the section (persona included) is omitted
  entirely — no persona bytes leak into any prompt.
"""
import pytest

from backend.agent import loop
from backend.tests.support_say import (  # noqa: F401 (restored_config is a fixture)
    restored_config,
    save_config_with_voice as _config_with_voice,
    say_section as _say_section,
)


# ---- enum -----------------------------------------------------------------


def test_persona_enum_is_fixed():
    """v1 ships exactly the three triaged personas; free-text customs are
    a follow-up issue. Guards accidental enum drift."""
    assert set(loop.SAY_PERSONAS) == {"neutral", "jester", "elitest"}


def test_say_persona_reads_neutral_for_absent_unset_unknown(restored_config):
    """/unset/unknown reads neutral (say_emissions precedent, no
    migration); casing and whitespace are normalized."""
    _config_with_voice()  # defaults: no say_persona key at all
    assert loop._say_persona() == "neutral"
    _config_with_voice(say_persona="")
    assert loop._say_persona() == "neutral"
    _config_with_voice(say_persona="pirate")
    assert loop._say_persona() == "neutral"
    _config_with_voice(say_persona="  JESTER  ")
    assert loop._say_persona() == "jester"
    _config_with_voice(say_persona=None)
    assert loop._say_persona() == "neutral"


# ---- neutral = today's bytes -----------------------------------------------


def test_neutral_prompt_is_byte_identical_to_absent_key(restored_config):
    """The acceptance criterion verbatim: neutral produces byte-identical
    prompts to today. An explicit 'neutral' must equal a voice dict with
    the key absent — which is exactly what existing users have on disk."""
    _config_with_voice()  # key absent
    without_key = loop._default_system_prompt("")
    _config_with_voice(say_persona="neutral")
    assert loop._default_system_prompt("") == without_key
    # And the section itself ends where it always did — no tail bytes.
    assert _say_section(without_key).endswith("a blank line marks a beat.\n")


def test_unknown_persona_falls_back_to_neutral_bytes(restored_config):
    """A garbage value (hand-edited config) must not change any prompt
    bytes — the fallback is the neutral prompt, not a mangled one."""
    _config_with_voice(say_persona="neutral")
    baseline = loop._default_system_prompt("")
    _config_with_voice(say_persona="gangster")
    assert loop._default_system_prompt("") == baseline


# ---- persona appends inside the say section --------------------------------


@pytest.mark.parametrize("persona", ["jester", "elitest"])
def test_persona_block_lands_inside_say_section_only(restored_config, persona):
    """jester/elitest change only the spoken-briefing section: the persona
    block sits between the section opening and the ask-user section, and
    everything before the say section is byte-identical to neutral."""
    _config_with_voice(say_persona="neutral")
    baseline = loop._default_system_prompt("")
    _config_with_voice(say_persona=persona)
    persona_prompt = loop._default_system_prompt("")

    say_opening = "Spoken briefing (voice read-aloud):\n"
    head = persona_prompt.split(say_opening, 1)[0] + say_opening
    assert head == baseline.split(say_opening, 1)[0] + say_opening
    assert f"Narrator persona — {persona}:" in _say_section(persona_prompt)
    # The persona block ends before the next section starts.
    assert f"Narrator persona — {persona}:" not in persona_prompt.split(
        "\nInterview the user", 1
    )[1]


@pytest.mark.parametrize("persona", ["jester", "elitest"])
def test_persona_block_carries_the_scope_boundary(restored_config, persona):
    """Hard scope boundary #1: each persona block explicitly states chat
    emissions are NEVER in that voice."""
    _config_with_voice(say_persona=persona)
    section = _say_section(loop._default_system_prompt(""))
    assert "ONLY the <say> briefing tag" in section
    assert "NEVER written in this voice" in section


def test_elitest_rudeness_ceiling_is_in_the_persona_text(restored_config):
    """Hard scope boundary #3: elitest never crosses into abuse — the
    ceiling (problem-directed smugness, never the user, no slurs or
    profanity) is written into the persona prompt text itself."""
    text = loop.SAY_PERSONAS["elitest"]
    assert "never at the user" in text or "NEVER at the user" in text
    assert "no slurs, no profanity" in text


# ---- emissions gate ---------------------------------------------------------


def test_persona_omitted_entirely_when_emissions_disabled(restored_config):
    """With say_emissions off, the section is omitted entirely — no
    persona prompt bytes leak into any prompt."""
    _config_with_voice(say_emissions=False, say_persona="jester")
    prompt = loop._default_system_prompt("")
    assert "Spoken briefing" not in prompt
    assert "Narrator persona" not in prompt
    assert "jester" not in prompt
