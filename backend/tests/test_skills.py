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
    # #193: unknown names are skipped here (reported as events by the loop,
    # via split_known_unknown) so they never enter the authoritative block.
    bodies = skill_registry.bodies_for_prompt(["review", "nope"])
    assert "# Skill: review" in bodies
    assert "Review code carefully." in bodies
    assert "nope" not in bodies
    known, unknown = skill_registry.split_known_unknown(["review", "nope"])
    assert known == ["review"]
    assert unknown == ["nope"]


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
    # a fake bundled-source dir with one shippable skill (multi-file)
    src = tmp_path / "bundled"
    (src / "grill-me").mkdir(parents=True)
    (src / "grill-me" / "SKILL.md").write_text(
        "---\nname: grill-me\ndescription: interview\ndisable-model-invocation: true\n---\n\nCall the Skill tool with \"grilling\".\n",
        encoding="utf-8",
    )
    (src / "grill-me" / "extra.md").write_text("supporting file", encoding="utf-8")
    monkeypatch.setattr(skill_registry, "bundled_source_dir", lambda: src)
    assert skill_registry.ensure_dir() is True
    assert d.is_dir()
    sample = d / "example" / "SKILL.md"
    assert sample.is_file()
    # the seeded sample must itself parse as a valid skill
    skill = skill_registry.parse_skill_md(sample)
    assert skill is not None
    assert skill.name == "example"
    # bundled skills are seeded whole — SKILL.md plus supporting files
    seeded = d / "grill-me"
    assert (seeded / "SKILL.md").is_file()
    assert (seeded / "extra.md").is_file()
    skill = skill_registry.parse_skill_md(seeded / "SKILL.md")
    assert skill is not None
    assert skill.name == "grill-me"
    # a pre-existing dir is left alone (no second sample write)
    mtime = sample.stat().st_mtime_ns
    assert skill_registry.ensure_dir() is False
    assert sample.stat().st_mtime_ns == mtime


def test_bundled_shipped_skills_parse():
    """Every skill actually shipped in backend/bundled_skills is valid."""
    src = skill_registry.bundled_source_dir()
    assert src is not None, "bundled skills missing from the repo"
    names = sorted(d.name for d in src.iterdir() if d.is_dir())
    assert len(names) >= 25
    for name in names:
        skill = skill_registry.parse_skill_md(src / name / "SKILL.md")
        assert skill is not None, f"{name}/SKILL.md does not parse"
        assert skill.name == name


PONYTAIL_SKILLS = [
    "ponytail",
    "ponytail-audit",
    "ponytail-debt",
    "ponytail-gain",
    "ponytail-help",
    "ponytail-review",
]


def test_ponytail_pack_shipped_and_parses():
    """#56: the ponytail pack is vendored as opt-in skills, pinned to the
    upstream commit recorded in PONYTAIL-PROVENANCE.md. The main skill uses
    a YAML block-scalar description; its name must still parse even under
    the no-PyYAML flat fallback."""
    src = skill_registry.bundled_source_dir()
    assert src is not None, "bundled skills missing from the repo"
    assert (src / "PONYTAIL-PROVENANCE.md").is_file()
    for name in PONYTAIL_SKILLS:
        skill = skill_registry.parse_skill_md(src / name / "SKILL.md")
        assert skill is not None, f"{name}/SKILL.md does not parse"
        assert skill.name == name
    main = skill_registry.parse_skill_md(src / "ponytail" / "SKILL.md")
    assert main is not None
    assert "minimal" in main.description.lower()


def test_ensure_dir_existing_dir_only_adds_bundled(skills_dir, monkeypatch):
    """An existing dir is not reseeded wholesale — the only change allowed
    is adding bundled skills that are missing."""
    src = skills_dir / "_bundled_src"
    src.mkdir()
    make_skill(src, "grill-me", description="interview")
    monkeypatch.setattr(skill_registry, "bundled_source_dir", lambda: src)
    before = sorted(p.name for p in skills_dir.iterdir())
    assert skill_registry.ensure_dir() is False
    after = sorted(p.name for p in skills_dir.iterdir())
    assert after == sorted(set(before) | {"grill-me"})


def test_ensure_dir_seeds_bundled_into_existing_dir(skills_dir, monkeypatch):
    """An upgraded install gets the bundled skills; user edits survive."""
    (skills_dir / "grilling").mkdir()
    (skills_dir / "grilling" / "SKILL.md").write_text(
        "---\nname: grilling\ndescription: mine\n---\n\nmy edit\n",
        encoding="utf-8",
    )
    src = skills_dir / "_bundled_src"
    src.mkdir()
    make_skill(src, "grill-me", description="interview")
    make_skill(src, "grilling", description="shipped version")
    monkeypatch.setattr(skill_registry, "bundled_source_dir", lambda: src)
    assert skill_registry.ensure_dir() is False
    assert (skills_dir / "grill-me" / "SKILL.md").is_file()
    assert "my edit" in (skills_dir / "grilling" / "SKILL.md").read_text(
        encoding="utf-8"
    )


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


# ---- issue #192: injection cap consistency ----------------------------------

def test_oversized_body_truncation_marked_in_parse_and_result(skills_dir):
    big = "z" * (skill_registry.MAX_SKILL_BODY_CHARS + 1000)
    make_skill(skills_dir, "big-skill", big)
    skill = skill_registry.scan_skills()["big-skill"]
    assert skill.body.endswith("…[truncated]")
    assert len(skill.body) <= skill_registry.MAX_SKILL_BODY_CHARS + len(
        "…[truncated]"
    )
    loaded = []
    messages = [{"role": "system", "content": "sys"}]
    result = skill_registry.load_skill_into_messages(
        {"name": "big-skill"}, loaded, messages
    )
    assert result.get("truncated") is True
    assert "…[truncated]" in messages[0]["content"]


def test_body_under_cap_not_marked(skills_dir):
    skill = skill_registry.scan_skills()["review"]
    assert not skill.body.endswith("…[truncated]")


def test_truncation_flag_true_only_when_actually_truncated(skills_dir):
    # CodeRabbit return trip #1 on PR #251: a skill whose body merely ENDS
    # with the truncation marker but is under the cap must not report
    # truncated=True — the flag reflects an actual cut.
    make_skill(skills_dir, "possum", "ends with the marker…[truncated]")
    loaded = []
    messages = [{"role": "system", "content": "sys"}]
    result = skill_registry.load_skill_into_messages(
        {"name": "possum"}, loaded, messages
    )
    assert result.get("truncated") is None


def test_pathological_description_clamped_in_index(skills_dir):
    make_skill(skills_dir, "loud", "Body.", description="D" * 5000)
    idx = skill_registry.index_for_prompt()
    line = next(ln for ln in idx.splitlines() if ln.startswith("- loud:"))
    assert len(line) <= 300
