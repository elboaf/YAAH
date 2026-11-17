"""#637 (t7-content): the Interceptor-era bundled skills ship and seed.

The content ticket of the browser-control spec (#630): the validated
interceptor-file-attach playbook is baked verbatim, the new YAAH-authored
browser-control-setup skill is model-invocable, and both seed into a fresh
skills dir through the normal bundled-skills loop.
"""
import json

from backend.agent import skills as skill_registry

# The validated draft ships verbatim: the SKILL.md's bytes up to the
# "YAAH packaging addendum" heading are byte-identical to the
# session-authored, live-validated draft (the session-authored file
# never lands in the repo, so this prefix hash pins it). The addendum
# after that marker carries environment adaptations CodeRabbit's
# rounds on #637 asked for (portable CLI path, -NoNewline payload
# build) WITHOUT touching the validated recipe itself.
FILE_ATTACH_VERBATIM_SHA256 = (
    "e6e40b4554435e69966a99525bcb6bc330e4583b5e6116b7ace6a445f2e5dc6f"
)


def _bundled():
    src = skill_registry.bundled_source_dir()
    assert src is not None, "bundled skills missing from the repo"
    return src


ADDENDUM_MARKER = "## YAAH packaging addendum"


def test_interceptor_file_attach_bundled_verbatim():
    """The session-authored, live-validated playbook ships byte-exact (its
    CRLF line endings are part of the validated artifact); environment
    adaptations live in an addendum after the validated body, never
    inside it."""
    import hashlib

    raw = (_bundled() / "interceptor-file-attach" / "SKILL.md").read_bytes()
    marker = ADDENDUM_MARKER.encode("utf-8")
    cut = raw.index(marker)
    verbatim_prefix = raw[:cut]
    assert (
        hashlib.sha256(verbatim_prefix).hexdigest()
        == FILE_ATTACH_VERBATIM_SHA256
    ), "validated draft body was modified -- adaptations belong in the addendum"
    # The addendum itself is present and carries the portability notes.
    addendum = raw[cut:].decode("utf-8")
    assert "-NoNewline" in addendum
    assert "$LOCALAPPDATA" in addendum
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
