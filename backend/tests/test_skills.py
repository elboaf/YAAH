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


def test_ensure_dir_creates_dir_and_sample(tmp_path, monkeypatch):
    d = tmp_path / "fresh" / "skills"
    monkeypatch.setattr(skill_registry, "SKILLS_DIR", d)
    assert skill_registry.ensure_dir() is True
    assert d.is_dir()
    sample = d / "example" / "SKILL.md"
    assert sample.is_file()
    # the seeded sample must itself parse as a valid skill
    skill = skill_registry.parse_skill_md(sample)
    assert skill is not None
    assert skill.name == "example"
    # a pre-existing dir is left alone (no second sample write)
    mtime = sample.stat().st_mtime_ns
    assert skill_registry.ensure_dir() is False
    assert sample.stat().st_mtime_ns == mtime


def test_ensure_dir_existing_dir_noop(skills_dir):
    before = sorted(p.name for p in skills_dir.iterdir())
    assert skill_registry.ensure_dir() is False
    assert sorted(p.name for p in skills_dir.iterdir()) == before


def test_bodies_include_skill_folder(skills_dir):
    skill_registry.scan_skills()
    bodies = skill_registry.bodies_for_prompt(["review"])
    assert "# Skill: review" in bodies
    assert str(skills_dir / "review") in bodies


def test_load_skill_result_includes_folder(skills_dir):
    skill_registry.scan_skills()
    messages = [{"role": "system", "content": "base"}]
    result = asyncio.run(_load_skill({"name": "review"}, [], messages))
    assert result["folder"] == str(skills_dir / "review")
    assert str(skills_dir / "review") in messages[0]["content"]


def test_resolve_path_allows_skill_reads_but_not_writes(tmp_path, monkeypatch):
    from backend.agent.tools import resolve_path

    skills_root = tmp_path / "sk"
    (skills_root / "review").mkdir(parents=True)
    monkeypatch.setenv("YAAH_SKILLS_PATH", str(skills_root))

    workspace = tmp_path / "ws"
    workspace.mkdir()

    # read: inside workspace fine, inside skills root fine, outside both no
    assert resolve_path(str(workspace), "src/a.py") == (workspace / "src" / "a.py").resolve()
    p = resolve_path(str(workspace), str(skills_root / "review" / "SKILL.md"))
    assert p == (skills_root / "review" / "SKILL.md").resolve()
    with pytest.raises(ValueError):
        resolve_path(str(workspace), str(tmp_path / "elsewhere" / "x.txt"))

    # write: skills root is blocked like anywhere else outside the workspace
    assert resolve_path(str(workspace), "src/a.py", for_write=True) == (
        workspace / "src" / "a.py"
    ).resolve()
    with pytest.raises(ValueError):
        resolve_path(str(workspace), str(skills_root / "review" / "SKILL.md"), for_write=True)
