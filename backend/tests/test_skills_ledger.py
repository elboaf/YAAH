"""Curated skill-repo ledger (#57 Phase 3 seed): schema, loading, lookups."""
import json

import pytest

from backend.agent import skills_ledger


def test_bundled_ledger_loads_and_is_valid():
    ledger = skills_ledger.load_bundled_ledger()
    # The 10 audited repos from #57 are the seed entries, plus
    # mattpocock/skills v1.3.1 (#315: pr + retro bundled).
    assert len(ledger) == 11
    for entry in ledger:
        assert entry["repo"]  # owner/name
        assert entry["verdict"] in ("A", "B", "C")
        assert entry["title"]
        assert isinstance(entry["notes"], str) and entry["notes"]


def test_bundled_ledger_repos_are_unique_and_wellformed():
    ledger = skills_ledger.load_bundled_ledger()
    repos = [e["repo"] for e in ledger]
    assert len(repos) == len(set(repos))
    for repo in repos:
        owner, _, name = repo.partition("/")
        assert owner and name, repo


def test_lookup_by_repo():
    entry = skills_ledger.lookup("obra/superpowers")
    assert entry is not None
    assert entry["verdict"] == "A"
    assert skills_ledger.lookup("nobody/nothing") is None


def test_verdict_counts():
    counts = skills_ledger.verdict_counts()
    assert counts == {"A": 7, "B": 3, "C": 1}


def test_mattpocock_pr_and_retro_are_bundled_and_parse():
    # #315: pr + retro vendored from mattpocock/skills v1.3.1.
    entry = skills_ledger.lookup("mattpocock/skills")
    assert entry is not None and entry["verdict"] == "A"
    from backend.agent.skills import bundled_source_dir, parse_skill_md

    src = bundled_source_dir()
    assert src is not None
    for name, model_invocable in (("pr", True), ("retro", False)):
        skill = parse_skill_md(src / name / "SKILL.md")
        assert skill is not None, name
        assert skill.name == name
        assert skill.disable_model_invocation is not model_invocable
        assert not skill.truncated


def test_load_ledger_from_explicit_path(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps([
        {"repo": "a/b", "verdict": "A", "title": "t", "notes": "n"},
    ]), encoding="utf-8")
    assert len(skills_ledger.load_ledger(p)) == 1


def test_load_ledger_tolerates_bad_entries(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps([
        {"repo": "ok/repo", "verdict": "A", "title": "t", "notes": "n"},
        {"repo": "bad", "verdict": "Z", "title": "", "notes": ""},
        "junk",
    ]), encoding="utf-8")
    ledger = skills_ledger.load_ledger(p)
    assert [e["repo"] for e in ledger] == ["ok/repo"]
