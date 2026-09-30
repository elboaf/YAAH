"""Tests for the prompt-manifest harness (issue #161).

The determinism guarantee has two layers: rendering the same combo twice
in-process must produce byte-identical manifests, and regenerating must
reproduce the COMMITTED manifest files byte-for-byte -- which is also the
drift guard: any prompt change that alters assembled output fails here
until the manifests are regenerated in the same PR.
All tests are sync on purpose: the harness drives asyncio.run internally.
"""
from pathlib import Path

from backend.agent import prompt_manifest as pm

MANIFEST_DIR = Path(__file__).parents[1] / "prompt_manifests"

# One representative per combo family, so every renderer participates in
# the determinism and drift checks without re-running the whole matrix
# twice inside the test suite.
REPRESENTATIVES = [
    "posix-local",
    "win-local-plan-compaction",
    "win-remote-compaction",
    "posix-remote-plan-skills-memory-compaction-sandboxonly",
    "win-remote-offline-normal",
    "kind-subagents-win-skills",
    "kind-auxiliary-prompts",
]


def test_matrix_shape():
    combos = pm.iter_combos()
    assert len(combos) == 215
    assert len(set(combos)) == len(combos)
    assert "win-local-plan-compaction" in combos
    assert "posix-remote-offline-normal" in combos
    assert "kind-subagents-posix-skills" in combos
    assert "kind-auxiliary-prompts" in combos


def test_render_is_deterministic_in_process():
    for combo in REPRESENTATIVES:
        first = pm.manifest_to_json(pm.render_combo(combo))
        second = pm.manifest_to_json(pm.render_combo(combo))
        assert first == second, f"nondeterministic render: {combo}"


def test_committed_manifests_match_regeneration():
    """Drift guard: committed manifests == what the current code renders."""
    for combo in REPRESENTATIVES:
        committed = (MANIFEST_DIR / f"{combo}.json").read_bytes()
        fresh = pm.manifest_to_json(pm.render_combo(combo)).encode("utf-8")
        assert committed == fresh, (
            f"committed manifest out of date for {combo}; regenerate with "
            "python -m backend.agent.prompt_manifest --all"
        )


def test_windows_sections_present_only_on_windows():
    win = pm.render_combo("win-local-compaction")
    posix = pm.render_combo("posix-local")
    win_names = [s["name"] for s in win["sections"]]
    posix_names = [s["name"] for s in posix["sections"]]
    assert "computer-use" in win_names
    assert "windows-sandbox" in win_names
    assert "computer-use" not in posix_names
    assert "windows-sandbox" not in posix_names


def test_skills_axis_flips_skills_index_section():
    with_skills = pm.render_combo("win-local-compaction")
    without = pm.render_combo("win-local-noskills-compaction")
    with_names = [s["name"] for s in with_skills["sections"]]
    without_names = [s["name"] for s in without["sections"]]
    assert "skills-index" in with_names
    assert "skills-index" not in without_names


def test_memory_axis_flips_memory_section():
    with_memory = pm.render_combo("win-local-compaction")
    without = pm.render_combo("win-local-nomemory-compaction")
    with_names = [s["name"] for s in with_memory["sections"]]
    without_names = [s["name"] for s in without["sections"]]
    assert "persistent-memory" in with_names
    assert "persistent-memory" not in without_names


def test_override_replaces_base_prompt_wholesale():
    overridden = pm.render_combo("win-local-override-compaction")
    plain = pm.render_combo("win-local-compaction")
    over_names = [s["name"] for s in overridden["sections"]]
    plain_names = [s["name"] for s in plain["sections"]]
    assert "override" in over_names
    assert "identity" not in over_names
    assert "identity" in plain_names
    assert "override" not in plain_names


def test_screenshot_axis_flips_screenshot_tool():
    shot = pm.render_combo("win-local-compaction")
    noshot = pm.render_combo("win-local-noshot-compaction")
    shot_tools = {t["name"] for t in shot["tool_schemas"]}
    noshot_tools = {t["name"] for t in noshot["tool_schemas"]}
    assert "screenshot" in shot_tools
    assert "screenshot" not in noshot_tools


def test_offline_note_only_for_offline_remote():
    offline = pm.render_combo("win-remote-offline-normal")
    online = pm.render_combo("win-remote-compaction")
    assert "the workspace's owning device is offline" in offline["rendered_text"]
    assert "the workspace's owning device is offline" not in online["rendered_text"]


def test_subagent_kind_covers_builtins():
    manifest = pm.render_combo("kind-subagents-win-skills")
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
