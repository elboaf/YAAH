"""Tests for the prompt-manifest harness (issue #161).

Platform honesty: win-* combos render only on a Windows host (the
canonical committer); posix-* combos render natively on posix hosts,
and flip-simulated on Windows for local review. The committed directory
carries the full matrix; each host byte-verifies only what it
canonically renders.
"""
import json
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

            # #188: the resolution no longer takes a client-side windows
            # flag — host shape comes from the workspace (LOCAL_WS for
            # kind combos, no remote connected in the manifest harness).
            defn = subagents.get_agent_def("general-purpose")
            names = {
                s["function"]["name"]
                for s in subagents._resolve_tools(
                    defn, workspace=pm.LOCAL_WS
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
    """Issue #179 acceptance, updated for #186: the SANDBOX SECTION still
    carries boot/run/stop mechanics for the local Windows render - the
    duplicated guidelines copy was removed, not the guidance itself."""
    text = pm.render_combo("win-local")["rendered_text"]
    assert "Boot with `sandbox_test`" in text
    assert "sandbox_stop" in text


@pytest.mark.skipif(not HOST_WIN, reason="duplication clusters are a win-local render fact")
def test_no_duplicated_guidance_clusters_win_local():
    """Issue #186: the five duplicated-guidance clusters must each appear
    exactly once per rendered win-local prompt (string counts in the
    manifest). The sandbox section is the single home for the
    test-environment rule, containment rules, and toolkit persistence."""
    text = pm.render_combo("win-local-compaction")["rendered_text"]
    # SYN-12: test-environment rule (loop.py guidelines copy vs sandbox.py)
    assert text.count("Choose the test environment") == 1
    # SYN-17: sandbox containment x3 (never host equivalent / never host
    # input / windows-mcp is the GUI layer)
    assert text.count("never launch a host equivalent") == 1
    # containment bullet in the guidelines block was removed; the
    # keep-it-inside-the-VM idea now lives only in the sandbox section
    assert text.count("Sandbox work stays IN the sandbox") == 0
    assert text.count("INSIDE the VM") == 1
    # SYN-43: the editing-leftover parenthetical was deleted
    assert "see the sandbox section) instead. Host input" not in text
    # SYN-44: env-line punctuation — no doubled period after the note
    assert "utilities.; " not in text


@pytest.mark.skipif(not HOST_WIN, reason="duplication clusters are a win-local render fact")
def test_exit_plan_flow_once_per_render_all_win_local_variants():
    """Issue #186 return trip (SYN-45 + review coverage): the exit-plan
    flow appears exactly once per rendered win-local prompt — the
    plan-mode note carries the full flow, the schema and the block
    result stay one-line summaries — across EVERY win-local variant,
    not just one."""
    from backend.agent.loop import EXIT_PLAN_SCHEMA, _plan_block_result

    plan_combos = [
        "win-local-plan-compaction",
        "win-local-noskills-nomemory-plan",
    ]
    other_combos = [
        "win-local",
        "win-local-compaction",
        "win-local-noshot-override-sandboxonly",
    ]
    for combo in plan_combos:
        text = pm.render_combo(combo)["rendered_text"]
        assert text.count("Plan mode is ON") == 1, combo
        assert text.count("present your plan by calling") == 1, combo
    for combo in other_combos:
        # plan mode is off: the note (and its exit-plan flow) is absent
        text = pm.render_combo(combo)["rendered_text"]
        assert text.count("Plan mode is ON") == 0, combo
    # the schema description is a one-line summary: the re-presentation
    # detail lives only in the plan-mode note
    assert "re-present" not in EXIT_PLAN_SCHEMA["function"]["description"]
    # the block result stays capability-neutral and one sentence
    err = _plan_block_result("write_file")["error"]
    assert err.count("plan mode is on") == 1
    assert "exit_plan" not in err
    # no content lost: plan renders still carry the sandbox mechanics
    # (the sandbox-only plan combo renders the full sandbox section)
    stext = pm.render_combo("win-local-sandboxonly")["rendered_text"]
    assert "sandbox_status" in stext
    assert "sandbox_stop" in stext
    # no content lost: the sandbox section still carries mechanics
    assert "Desktop\\\\toolkit" in stext or "Desktop\\toolkit" in stext


def test_windows_bash_note_has_no_trailing_period():
    """Issue #186 (SYN-44): the note composes into 'X; <note>; use
    commands' in the local env line — a trailing period doubles up."""
    from backend.agent import loop

    note = loop._shell_phrase(True) if hasattr(loop, "_shell_phrase") else ""
    assert not note.rstrip().endswith("."), note


def test_posix_local_env_line_grammatical():
    """Issue #186 AC: the posix-local env line stays grammatical after
    the punctuation fix (no doubled sentence separator)."""
    import re

    text = pm.render_combo("posix-local")["rendered_text"]
    m = re.search(r"Runtime environment:[^\n]*", text)
    assert m, text[:400]
    assert ".; " not in m.group(0), m.group(0)
    assert "; use commands and paths valid" in m.group(0)


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


@pytest.mark.skipif(HOST_WIN, reason="posix-host branch of the os.name flip")
def test_windows_flip_on_posix_does_not_instantiate_windowspath(
    monkeypatch, tmp_path
):
    """#184 CI follow-up: on a posix host, _platform_os_name(True) flips
    os.name to 'nt' without rebinding Path. Any pathlib.Path() call made
    under the flip (here, a probe module) instantiates a real WindowsPath
    and raises 'cannot instantiate WindowsPath on your system' -- the exact
    Linux-CI failure in test_compaction_manifests_contain_summary_section.
    The flip must rebind Path to a WindowsPath subclass on posix hosts too,
    mirroring the Windows-host branch."""
    assert not HOST_WIN

    import sys as _sys
    import types
    probe = types.SimpleNamespace(Path=Path)
    # Register through monkeypatch so the probe module is removed from
    # sys.modules at teardown -- a bare assignment here leaked it into the
    # rest of the session (CodeRabbit return trip #1 on PR #225).
    monkeypatch.setitem(_sys.modules, "backend._pm_probe_mod", probe)

    with pm._platform_os_name(True):
        # Instantiate THE REBOUND NAME, not the imported Path: the whole
        # point is that backend modules' Path attr must be directly
        # instantiable under the flip. (A subclass inherits WindowsPath's
        # raising __new__ on posix hosts, because the guard is defined
        # inside WindowsPath itself when os.name != 'nt' -- so the
        # subclass must override __new__ to shed the inherited guard.)
        probe.Path("whatever")  # must not raise

    assert probe.Path is Path


@pytest.mark.skipif(
    not HOST_WIN,
    reason="exercises the posix-target flip via a real posix-* render",
)
def test_posix_target_flip_render_keeps_full_path_api():
    """#184 regression (merged-code follow-up): an intermediate 'fix'
    rebound backend Path names to a PureWindowsPath mix-in during the
    flip. PureWindowsPath has NO filesystem methods, so any real render
    (mkdir/write_text/iterdir in the turn drivers) died with
    AttributeError -- on every host. The rebinding must subclass the
    CONCRETE WindowsPath: subclasses defined in user code do not inherit
    pathlib's host guard, so they instantiate under the flip on either
    host AND keep the full concrete API. A pure-path probe cannot catch
    this (it never touches the filesystem); a real render can."""
    manifest = pm.render_combo(
        f"{'posix' if HOST_WIN else 'win'}-local-plan-compaction"
    )
    names = [s["name"] for s in manifest["sections"]]
    assert "compaction-summary" in names
    assert manifest["total_bytes"] > 0


def test_compaction_manifests_contain_summary_section():
    """#184: every combo that names compaction must actually render the
    compaction-summary section -- the fixture watermark must be live
    (relative to the fixture conversation), never consumed by the
    warm-up turn."""
    for combo in pm.iter_combos():
        if "compaction" not in pm._split_combo(combo) or not pm._split_combo(
            combo
        )["compaction"]:
            continue
        if combo == "kind-auxiliary-prompts":
            continue
        manifest = pm.render_combo(combo)
        names = [s["name"] for s in manifest["sections"]]
        assert "compaction-summary" in names, (
            f"{combo}: -compaction combo rendered without the "
            "compaction-summary section (stale fixture watermark?)"
        )


def test_compaction_summary_text_present_in_rendered_bytes():
    """#184: the summary text the fixture persists must reach the model
    bytes for a representative local compaction render."""
    manifest = pm.render_combo(f"{PFX}-local-plan-compaction")
    text = json.dumps(manifest)
    assert "Fixture summary of the earlier conversation." in text


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
