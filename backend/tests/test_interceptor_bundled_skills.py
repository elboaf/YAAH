"""#637 (t7-content): the Interceptor-era bundled skills ship and seed.

The content ticket of the browser-control spec (#630): the validated
interceptor-file-attach playbook is baked verbatim, the new YAAH-authored
browser-control-setup skill is model-invocable, and both seed into a fresh
skills dir through the normal bundled-skills loop.
"""
import json

from backend.agent import skills as skill_registry

# The validated draft this ticket bakes (byte-identity is the point of the
# "verbatim" acceptance criterion; the session-authored file never lands in
# the repo, so the hash pins the exact baked content instead). Deviations
# from the raw draft, each from a CodeRabbit round on #637: the recipe's
# EXE line resolves the install dir from the current user's LOCALAPPDATA
# (not the hard-coded Administrator profile), and the payload step carries
# the Set-Content trailing-CRLF guidance.
FILE_ATTACH_SHA256 = "d5bfce7d6f336503e952ecdb2823a80932ab3a4227f62d975fcbc5b25b22a1ad"


def _bundled():
    src = skill_registry.bundled_source_dir()
    assert src is not None, "bundled skills missing from the repo"
    return src


def test_interceptor_file_attach_bundled_verbatim():
    """The session-authored, live-validated playbook ships byte-exact (its
    CRLF line endings are part of the validated artifact)."""
    raw = (_bundled() / "interceptor-file-attach" / "SKILL.md").read_bytes()
    import hashlib

    assert hashlib.sha256(raw).hexdigest() == FILE_ATTACH_SHA256
    skill = skill_registry.parse_skill_md(
        _bundled() / "interceptor-file-attach" / "SKILL.md"
    )
    assert skill is not None
    assert skill.name == "interceptor-file-attach"
    assert skill.disable_model_invocation is False


def test_browser_control_setup_bundled_and_model_invocable():
    """The new YAAH-authored setup skill parses and carries NO
    disable-model-invocation flag: the agent must be able to load it itself
    to drive/coach setup (the ticket's explicit frontmatter requirement)."""
    skill = skill_registry.parse_skill_md(
        _bundled() / "browser-control-setup" / "SKILL.md"
    )
    assert skill is not None
    assert skill.name == "browser-control-setup"
    assert skill.disable_model_invocation is False
    # The verified Windows gotchas the spec names, present in the body.
    body = skill.body
    for needle in (
        "--format png",
        "--any-tab",
        "PowerShell 5.1",
        "diagnose extension",
        "/api/interceptor/status",
    ):
        assert needle in body, f"body missing verified gotcha: {needle}"


def test_new_skills_seed_into_fresh_skills_dir(tmp_path, monkeypatch):
    """A fresh skills dir gets both new skills through the ordinary
    ensure_dir() seeding loop (no special-casing)."""
    monkeypatch.setattr(skill_registry, "SKILLS_DIR", tmp_path)
    monkeypatch.setattr(skill_registry, "_scanned", False)
    skill_registry.ensure_dir()  # creates the dir; False (it just made it)
    for name in ("interceptor-file-attach", "browser-control-setup"):
        assert (tmp_path / name / "SKILL.md").is_file()
        skill = skill_registry.parse_skill_md(tmp_path / name / "SKILL.md")
        assert skill is not None, f"{name} did not seed as a valid skill"
        assert skill.name == name


def test_skills_ledger_entries_for_new_baked_skills():
    """The ledger's JSON stays valid and gains one entry per new baked
    skill, pointing at the shipping repo (the skills-ledger module was
    ponytail-deleted; the committed asset is the record)."""
    entries = json.loads(
        (_bundled() / "skills-ledger.json").read_text(encoding="utf-8")
    )
    repos = [e["repo"] for e in entries]
    assert "elboaf/Interceptor (in-repo draft)" in repos
    assert "elboaf/YAAH (authored in-repo)" in repos
    for e in entries:
        assert e["verdict"] in ("A", "B", "C")
        assert e["title"] and e["notes"]
