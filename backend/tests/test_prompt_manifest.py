"""Tests for the prompt-manifest harness (issue #161).

Platform honesty: win-* combos render only on a Windows host (the
canonical committer); posix-* combos render natively on posix hosts,
and flip-simulated on Windows for local review. The committed directory
carries the full matrix; each host byte-verifies only what it
canonically renders.
"""
from pathlib import Path

import pytest

from backend.agent import prompt_manifest as pm

MANIFEST_DIR = Path(__file__).parents[1] / "prompt_manifests"
HOST_WIN = pm.HOST_WINDOWS
PFX = "win" if HOST_WIN else "posix"

# One representative per combo family, host-platform-correct, so every
# renderer participates in the determinism checks without re-running
# the whole matrix twice inside the test suite.
REPRESENTATIVES = [
    f"{PFX}-local",
    f"{PFX}-local-plan-compaction",
    f"{PFX}-local-override-compaction",
    f"{PFX}-remote-plan-skills-memory-compaction-sandboxonly",
    f"{PFX}-remote-offline-plan",
    f"kind-subagents-{PFX}-skills",
    "kind-auxiliary-prompts",
]


def test_git_editor_recipe_rendered_exactly_once_per_win_local_combo():
    """Issue #187 SYN-13 return trip: the full git-editor recipe ("pass -m
    '<message>' to git commit" + GIT_EDITOR=true) lives in exactly ONE
    rendered schema location per win-local render, even though both the
    bash and powershell schemas render. Host-independent: builds the
    win-local schema set directly from tools.get_schemas' documented
    platform shape (local = base + Windows-only schemas)."""
    import sys

    import backend.agent.tools as tools_mod

    schemas = list(tools_mod.TOOLS_SCHEMA)
    if sys.platform == "win32":
        schemas.append(tools_mod.POWERSHELL_SCHEMA)
        names = {s["function"]["name"] for s in schemas}
        assert {"bash", "powershell"} <= names
    else:
        pytest.skip(
            "win-local combos render the powershell schema only on Windows"
        )
    recipe_count = sum(
        s["function"]["description"].count("to git commit")
        for s in schemas
    )
    assert recipe_count == 1, (
        "the full git-editor recipe must appear in exactly one schema "
        "description; pointer copies must not carry it"
    )
    # The canonical home keeps the complete rule (both halves).
    assert any(
        "GIT_EDITOR=true" in s["function"]["description"]
        for s in schemas
    )


def test_matrix_shape():
    combos = pm.iter_combos()
    assert len(combos) == 215
    assert len(set(combos)) == len(combos)
    assert "win-local-plan-compaction" in combos
    assert "posix-remote-offline-normal" in combos
    assert "kind-subagents-posix-skills" in combos
    assert "kind-auxiliary-prompts" in combos


def test_host_filter_respects_platform():
    for combo in pm.combos_for_host():
        assert pm._combo_targets_windows(combo) == pm.HOST_WINDOWS, combo


def test_render_is_deterministic_in_process():
    for combo in REPRESENTATIVES:
        first = pm.manifest_to_json(pm.render_combo(combo))
        second = pm.manifest_to_json(pm.render_combo(combo))
        assert first == second, f"nondeterministic render: {combo}"


@pytest.mark.skipif(
    not HOST_WIN,
    reason=(
        "byte-level drift guard runs on the canonical Windows host; "
        "committed posix manifests are Windows-flip reference bytes "
        "(native regeneration is a synthesis-ticket decision)"
    ),
)
def test_committed_manifests_match_regeneration():
    """Drift guard: committed manifests == what the current code renders."""
    for combo in REPRESENTATIVES:
        committed = (MANIFEST_DIR / f"{combo}.json").read_bytes()
        fresh = pm.manifest_to_json(pm.render_combo(combo)).encode("utf-8")
        assert committed == fresh, (
            f"committed manifest out of date for {combo}; regenerate with "
            "python -m backend.agent.prompt_manifest --all"
        )


def test_platform_sections_match_host_prefix():
    """Windows-only sections appear in win renders, never in posix ones
    (flipped on a Windows host, native elsewhere)."""
    if HOST_WIN:
        win = pm.render_combo("win-local-compaction")
        posix = pm.render_combo("posix-local")
        win_names = [s["name"] for s in win["sections"]]
        posix_names = [s["name"] for s in posix["sections"]]
        assert "computer-use" in win_names
        assert "windows-sandbox" in win_names
        assert "computer-use" not in posix_names
        assert "windows-sandbox" not in posix_names
    else:
        posix = pm.render_combo("posix-local")
        posix_names = [s["name"] for s in posix["sections"]]
        assert "computer-use" not in posix_names
        assert "windows-sandbox" not in posix_names




def test_no_glued_section_headers():
    """#173: a fragment joined without its separator glues the next
    section's markdown H1 mid-line. No '#' section opening may ever sit
    after a non-newline character in rendered text."""
    import re

    for combo in REPRESENTATIVES:
        text = pm.render_combo(combo)["rendered_text"]
        glued = re.search(r"(?<![#\n])# ", text)
        assert not glued, (combo, glued.group(0) if glued else "")


@pytest.mark.skipif(
    not HOST_WIN,
    reason="sandbox-section separator is a win-local render fact",
)
def test_sandbox_fragment_uses_standard_separator():
    """#173: the sandbox fragment joins with the same SEPARATOR as every
    other appended fragment, never raw."""
    text = pm.render_combo("win-local-compaction")["rendered_text"]
    assert pm.SEPARATOR + "# Windows Sandbox" in text


def test_no_unactionable_sandbox_guidance():
    """Issue #179: no rendered combo may instruct the model to use
    sandbox tooling that its schema set does not carry. Sandbox tools
    exist only for a local Windows session (host is None and windows),
    so the sandbox bullets must render there and nowhere else —
    asserted per name against the rendered tool-schema list."""
    GUIDANCE_ONLY = ("sandbox_test", "sandbox_run", "sandbox_status",
                     "sandbox_stop")
    for combo in REPRESENTATIVES + [f"{PFX}-local-plan"]:
        manifest = pm.render_combo(combo)
        names = {t["name"] for t in manifest["tool_schemas"]}
        # Sub-agent kind combos render a general-purpose sub-agent's
        # prompt; its prose derives from the sub-agent's own tool
        # resolution (#181), not the combo-level schema list (which is
        # empty for these combos).
        if combo.startswith("kind-subagents"):
            from backend.agent import subagents

            defn = subagents.get_agent_def("general-purpose")
            names = {
                s["function"]["name"]
                for s in subagents._resolve_tools(
                    defn, windows=combo.startswith("kind-subagents-win")
                )
            }
        # Remote combos carry the remote runner's own hand-maintained
        # prose tool list (remote_runner.py), whose drift is issue #181's
        # scope — #179 covers the loop's guidelines block.
        if "remote" in combo:
            continue
        for tool in GUIDANCE_ONLY:
            if tool not in names:
                assert tool not in manifest["rendered_text"], (
                    f"{combo}: prompt names '{tool}' but no schema carries it"
                )
        # windows-mcp is a server, not a schema tool: it is only actionable
        # when the sandbox integration it belongs to is present.
        if "sandbox_run" not in names:
            assert "windows-mcp" not in manifest["rendered_text"], combo


@pytest.mark.skipif(
    not HOST_WIN, reason="win-local render facts are canonical on Windows"
)
def test_win_local_keeps_sandbox_guidance():
    """Issue #179 acceptance: Windows local renders are unchanged."""
    text = pm.render_combo("win-local")["rendered_text"]
    assert "Boot with sandbox_test and run commands via sandbox_run" in text


def test_skills_axis_flips_skills_index_section():
    with_skills = pm.render_combo(f"{PFX}-local-compaction")
    without = pm.render_combo(f"{PFX}-local-noskills-compaction")
    with_names = [s["name"] for s in with_skills["sections"]]
    without_names = [s["name"] for s in without["sections"]]
    assert "skills-index" in with_names
    assert "skills-index" not in without_names


def test_memory_axis_flips_memory_section():
    with_memory = pm.render_combo(f"{PFX}-local-compaction")
    without = pm.render_combo(f"{PFX}-local-nomemory-compaction")
    with_names = [s["name"] for s in with_memory["sections"]]
    without_names = [s["name"] for s in without["sections"]]
    assert "persistent-memory" in with_names
    assert "persistent-memory" not in without_names


def test_override_replaces_base_prompt_wholesale():
    overridden = pm.render_combo(f"{PFX}-local-override-compaction")
    plain = pm.render_combo(f"{PFX}-local-compaction")
    over_names = [s["name"] for s in overridden["sections"]]
    plain_names = [s["name"] for s in plain["sections"]]
    assert "override" in over_names
    assert "identity" not in over_names
    assert "identity" in plain_names
    assert "override" not in plain_names


@pytest.mark.skipif(not HOST_WIN, reason="screenshot axis exists only in win combos")
def test_screenshot_axis_flips_screenshot_tool():
    shot = pm.render_combo("win-local-compaction")
    noshot = pm.render_combo("win-local-noshot-compaction")
    shot_tools = {t["name"] for t in shot["tool_schemas"]}
    noshot_tools = {t["name"] for t in noshot["tool_schemas"]}
    assert "screenshot" in shot_tools
    assert "screenshot" not in noshot_tools


def test_offline_note_only_for_offline_remote():
    offline = pm.render_combo(f"{PFX}-remote-offline-plan")
    online = pm.render_combo(f"{PFX}-remote-plan-skills-memory-compaction-sandboxonly")
    assert "the workspace's owning device is offline" in offline["rendered_text"]
    assert "the workspace's owning device is offline" not in online["rendered_text"]


def test_remote_offline_plan_note_rendered_and_differs_from_normal():
    """Issue #178: the remote-offline wrapper must apply the plan-mode
    note exactly as the local path does, so plan-vs-normal offline
    combos differ in the plan-note section instead of being
    byte-identical while both carrying exit_plan."""
    plan = pm.render_combo(f"{PFX}-remote-offline-plan")
    normal = pm.render_combo(f"{PFX}-remote-offline-normal")
    assert "# Access mode: PLAN" in plan["rendered_text"]
    assert "# Access mode: PLAN" not in normal["rendered_text"]
    assert plan["rendered_sha256"] != normal["rendered_sha256"]


def test_plan_combos_differ_only_by_plan_note():
    """Issue #178 acceptance: combos differing only in `plan` differ in
    exactly the plan-note section (same tool schema names; the
    rendered system text differs by the note, not by anything else
    the plan flag was silently meant to control)."""
    pairs = [
        (f"{PFX}-remote-offline-normal", f"{PFX}-remote-offline-plan"),
        (f"{PFX}-local", f"{PFX}-local-plan"),
    ]
    for normal_id, plan_id in pairs:
        normal = pm.render_combo(normal_id)
        plan = pm.render_combo(plan_id)
        plan_names = [s["name"] for s in plan["tool_schemas"]]
        normal_names = [s["name"] for s in normal["tool_schemas"]]
        # The ONLY schema delta plan mode makes is appending exit_plan
        # (loop.py appends it when plan is active).
        assert plan_names == normal_names + ["exit_plan"], normal_id
        assert plan["rendered_text"].startswith(
            normal["rendered_text"]
        ) or plan["rendered_text"].endswith(normal["rendered_text"]), normal_id


def test_subagent_kind_covers_builtins():
    manifest = pm.render_combo(f"kind-subagents-{PFX}-skills")
    prompts = manifest["subagent_prompts"]
    assert set(prompts) == {"general-purpose", "explore"}
    for entry in prompts.values():
        assert entry["bytes"] > 0
        assert entry["sections"]


def test_auxiliary_kind_captures_both_prompts():
    manifest = pm.render_combo("kind-auxiliary-prompts")
    prompts = manifest["auxiliary_prompts"]
    assert "compaction_summarizer" in prompts
    assert "title_generation" in prompts
    for entry in prompts.values():
        assert entry["bytes"] > 0
        assert entry["text"]
