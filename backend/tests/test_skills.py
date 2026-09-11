"""Skills registry: parsing, scanning, prompt injection, load_skill tool."""
import asyncio
import json

import pytest

from backend.agent import skills as skill_registry
from backend.agent.loop import _default_system_prompt, _load_skill


def make_skill(tmp_path, name, body="Do the thing.", **meta_lines):
    d = tmp_path / name
    d.mkdir()
    fm = "---\nname: %s\n" % name
    for k, v in meta_lines.items():
        fm += f"{k}: {v}\n"
    fm += "---\n"
    (d / "SKILL.md").write_text(fm + "\n" + body, encoding="utf-8")
    return d


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    """Redirect the registry to a throwaway dir with two skills."""
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setattr(skill_registry, "SKILLS_DIR", d)
    monkeypatch.setattr(skill_registry, "_scanned", False)
    make_skill(d, "review", "Review code carefully.", description="Code review helper")
    make_skill(
        d, "manual-only", "Secret steps.", description="Never auto-load",
        **{"disable-model-invocation": "true"},
    )
    return d


def test_parse_and_scan(skills_dir):
    found = skill_registry.scan_skills()
    assert set(found) == {"review", "manual-only"}
    assert found["review"].description == "Code review helper"
    assert found["review"].body == "Review code carefully."
    assert found["manual-only"].disable_model_invocation is True


def test_broken_skill_does_not_break_scan(skills_dir):
    bad = skills_dir / "broken"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\n[unclosed\n---\nbody", encoding="utf-8")
    noname = skills_dir / "noname"
    noname.mkdir()
    (noname / "SKILL.md").write_text("---\ndescription: x\n---\nbody", encoding="utf-8")
    found = skill_registry.scan_skills()
    assert set(found) == {"review", "manual-only"}


def test_index_hides_manual_only(skills_dir):
    skill_registry.scan_skills()
    idx = skill_registry.index_for_prompt()
    assert "review" in idx
    assert "manual-only" not in idx
    assert "load_skill" in idx


def test_index_empty_when_no_skills(tmp_path, monkeypatch):
    empty = tmp_path / "none"
    empty.mkdir()
    monkeypatch.setattr(skill_registry, "SKILLS_DIR", empty)
    monkeypatch.setattr(skill_registry, "_scanned", False)
    assert skill_registry.index_for_prompt() == ""


def test_system_prompt_includes_skill_index(skills_dir):
    skill_registry.scan_skills()
    prompt = _default_system_prompt()
    assert "review" in prompt
    assert "load_skill" in prompt


def test_bodies_for_prompt(skills_dir):
    skill_registry.scan_skills()
    bodies = skill_registry.bodies_for_prompt(["review", "nope"])
    assert "# Skill: review" in bodies
    assert "Review code carefully." in bodies
    assert "# Skill not found: nope" in bodies


def test_load_skill_tool(skills_dir):
    skill_registry.scan_skills()
    loaded: list[str] = []
    messages = [{"role": "system", "content": "base prompt"}]

    result = asyncio.run(_load_skill({"name": "review"}, loaded, messages))
    assert result["loaded"] == "review"
    assert "# Loaded skill: review" in messages[0]["content"]
    assert "Review code carefully." in messages[0]["content"]

    # Second load of the same skill is a no-op on the prompt
    result = asyncio.run(_load_skill({"name": "review"}, loaded, messages))
    assert result.get("note") == "already loaded this turn"
    assert messages[0]["content"].count("# Loaded skill: review") == 1

    # Unknown skill reports what IS available; manual-only stays loadable
    # by the model only insofar as it knows the name — the registry itself
    # permits it, matching "hidden from the index, not forbidden".
    result = asyncio.run(_load_skill({"name": "zzz"}, loaded, messages))
    assert result["error"].startswith("Unknown skill")
    assert "review" in result["available"]


def test_load_skill_schema_registered():
    from backend.agent.tools import get_schemas

    names = [s["function"]["name"] for s in get_schemas()]
    assert "load_skill" in names
