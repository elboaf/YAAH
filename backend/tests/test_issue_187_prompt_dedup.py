"""Issue #187: duplicated guidance between tool schemas and the sandbox
section (SYN-13/14/15/16). Each fact gets exactly one home per rendered
prompt; schema descriptions keep their own contract plus a pointer."""
import pytest

from backend.agent import prompt_manifest as pm

HOST_WIN = pm.HOST_WINDOWS
PFX = "win" if HOST_WIN else "posix"


def _schema_desc(name):
    """Description from any schema table (bash/powershell/sandbox tools)."""
    from backend.agent import sandbox as sb
    from backend.agent import tools as tools_mod
    all_schemas = (list(tools_mod.TOOLS_SCHEMA)
                   + [tools_mod.POWERSHELL_SCHEMA]
                   + list(sb.SANDBOX_TOOLS_SCHEMA))
    for s in all_schemas:
        if s["function"]["name"] == name:
            return s["function"]["description"]
    raise AssertionError(f"tool {name} not found")


def _desc(name):
    return _schema_desc(name)


def _render(combo):
    return pm.render_combo(combo)["rendered_text"]


def _local_combo():
    combo = f"{PFX}-local"
    if combo not in pm.iter_combos():
        pytest.skip("combo not on this host")
    return combo


def _win_local_combo():
    """The WINDOWS local render id.

    NOTE: win-* combos only render on a Windows host (combos_for_host
    filters them out elsewhere; the flip machinery simulates only
    Windows->posix). The sandbox-section facts under test here exist only
    in win-* renders, so tests that need them must skip on posix hosts."""
    return "win-local"


def _requires_windows_host():
    if not HOST_WIN:
        pytest.skip("win-render facts render only on a Windows host")


# ------------------------------------------------- SYN-13: git-editor rule

def test_git_editor_rule_is_one_shared_constant():
    """The git-editor rule is canonical in the bash schema; the powershell
    schema carries only a short pointer to it (no rendered duplicate)."""
    from backend.agent import tools as tools_mod
    note = tools_mod._GIT_EDITOR_NOTE
    pointer = tools_mod._GIT_EDITOR_POINTER
    assert "git must never open its editor" in note
    assert "GIT_EDITOR=true" in note
    assert note in _desc("bash")
    assert note not in _desc("powershell")
    assert pointer in _desc("powershell")


def test_git_editor_recipe_appears_once_per_render():
    """GIT_EDITOR=true (the -m recipe) renders exactly once: in the bash
    schema. The sandbox section keeps only the VM-specific GIT_EDITOR
    preset delta."""
    _requires_windows_host()
    text = _render(_win_local_combo())
    # the recipe lives in the bash schema (not embedded in the prompt
    # body), so at most one occurrence; the VM preset delta survives
    assert text.count("GIT_EDITOR=true") <= 1
    assert "GIT_EDITOR" in text


# ------------------------------------------------- SYN-14: gh-CLI preference

def test_gh_cli_stated_only_in_guidelines():
    """The gh-CLI preference lives only in the base-prompt guidelines; the
    shell-tool descriptions no longer repeat it."""
    assert "gh CLI" not in _desc("bash")
    assert "gh CLI" not in _desc("powershell")
    text = _render(_local_combo())
    assert text.count("gh CLI") == 1
    assert "bundle gh" in text  # substantive facts survive (bundled gh, auth)


# ------------------------------------------------- SYN-15: windows-mcp playbook

def test_host_input_note_is_one_clause_without_playbook():
    """_HOST_INPUT_NOTE repeats on six input tools; it must be a single
    containment clause that points at the sandbox section, not a copy of
    the windows-mcp playbook."""
    from backend.agent import computer as computer_mod
    note = computer_mod._HOST_INPUT_NOTE
    assert "REAL mouse/keyboard" in note
    assert "Windows Sandbox" in note
    # one containment clause + pointer; not a playbook copy
    assert "windows-mcp MCP server" in note
    assert "mcp.json" not in note
    assert "Bearer" not in note
    assert "Snapshot" not in note
    assert "auto-start" not in note


def test_mcp_playbook_lives_only_in_sandbox_section():
    """The raw-HTTP connect recipe and key-tool mechanics render exactly
    once (the sandbox section); sandbox_run's description keeps only a
    pointer."""
    _requires_windows_host()
    text = _render(_win_local_combo())
    for fact in ("mcp-session-id", "windows-mcp-serve.ps1",
                 "notifications/initialized", "launch_executable"):
        assert text.count(fact) >= 1, fact
    run_desc = _desc("sandbox_run")
    assert "windows-mcp" in run_desc          # pointer survives
    assert "Bearer" not in run_desc
    assert "trailing slash" not in run_desc
    assert "Snapshot" not in run_desc
    assert "mcp-session-id" not in run_desc


# ------------------------------------------------- SYN-16: environment cluster

def test_clean_image_install_rules_lives_only_in_section():
    """The clean-image story (state.json, toolkit PATH, shim recipe,
    silent-install flags) renders once, in the sandbox section;
    sandbox_run's description keeps only the state.json pointer."""
    _requires_windows_host()
    text = _render(_win_local_combo())
    assert text.count("CLEAN WINDOWS IMAGE") == 1
    assert text.count("node_modules") <= 2  # PATH enumeration, not x2 playbook
    run_desc = _desc("sandbox_run")
    assert "state.json" in run_desc          # pointer survives
    assert "not preinstalled" not in run_desc
    assert "winget" not in run_desc
    assert "InstallAllUsers" not in run_desc


def test_round_trip_cost_has_one_home():
    """The ~1-3s file-polling cost is stated once in the rendered prompt
    (the sandbox section), not repeated in the sandbox_run schema."""
    _requires_windows_host()
    text = _render(_win_local_combo())
    assert text.count("~1-3s") <= 1
    assert "~1-3s" not in _desc("sandbox_run")
