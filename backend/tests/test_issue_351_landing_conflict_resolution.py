"""Issue #351: landing (merge with conflict resolution) as the default.

Under ADR-0014 a landing hit two walls that reproduced the dead air
#349 removed: a dirty primary froze it as manual-only, and a merge
conflict aborted it with a rebase offer - the user then had to type
free text ("can you merge with conflict resolution please") to get
what they assumed was one of the options. ADR-0015 upgrades "Land in
`<branch>`" in place: dirty-primary WIP is swept to a local `wip/`
branch (never stashed, never pushed), conflicts resolve with the
run-branch side winning on every landing path, and only a primary
mid-merge/rebase/cherry-pick still freezes. Pure prompt contract:
same seam as #349, the selector notes and the ask block.
"""

import pytest

from backend.agent import loop


def test_normal_note_resolves_instead_of_abort():
    note = loop._selected_branch_note("bigtest", 7)
    # The landing merge happens inside this chat's private worktree;
    # on conflict the agent resolves - the run branch's side wins -
    # and never aborts the landing.
    assert "git merge" in note
    assert "run branch's side wins" in note
    assert "-X theirs" in note
    assert "merge --abort" not in note
    # Every conflict resolution is reported.
    assert "landing report" in note


def test_normal_note_landing_ask_names_resolution():
    note = loop._selected_branch_note("bigtest", 7)
    # Option 1 of the ask describes the resolution-capable landing.
    assert "End-of-work landing ask" in note
    assert "conflicts resolve" in note.lower()


def test_detached_note_sweeps_dirty_primary_to_wip_branch():
    note = loop._selected_branch_note("master", 7, detached=True)
    # Ordinary dirty-primary WIP is preserved on a LOCAL wip branch -
    # a commit, not a stash - so the landing proceeds (ADR-0015).
    assert "wip/" in note
    assert "add -A" in note
    assert "write-tree" in note
    assert "commit-tree" in note
    assert "stashed" not in note.lower() or "not stashed" in note.lower()
    # The sweep is checkout-free: the primary is never switched.
    assert "never `git switch`" in note
    # The wip branch is local-only, like run branches (#320).
    assert "never `git push`" in note
    # The report names the branch and the restore command.
    assert "cherry-pick" in note
    assert "restore" in note.lower()


def test_detached_note_still_freezes_mid_merge_primary():
    note = loop._selected_branch_note("master", 7, detached=True)
    # The one remaining freeze: a primary mid merge/rebase/cherry-pick
    # is a half-finished human operation - never swept, never unwound.
    # All three signals, not just MERGE_HEAD.
    assert "MERGE_HEAD" in note
    assert "CHERRY_PICK_HEAD" in note
    assert "rebase-merge" in note
    assert "manual-only" in note.lower()


def test_detached_note_diverged_landing_resolves_conflicts():
    note = loop._selected_branch_note("master", 7, detached=True)
    # The plumbing merge uses the --merge-base form: positional
    # base-as-branch1 would merge the wrong sides.
    assert "merge-tree --write-tree --merge-base=" in note
    assert f"-X theirs" in note
    assert "run branch's side wins" in note
    # Land BEFORE the ref move: the merge parents the pre-landing tip.
    assert "before" in note.lower()
    assert "update-ref refs/heads/master <landing tip>" in note


def test_detached_note_never_switches_the_primary():
    note = loop._selected_branch_note("master", 7, detached=True)
    # A checkout-free sweep: the landing SOP may name the rule ("never
    # `git switch` in the primary") but must contain no switch step.
    sop = note.split("1. FREEZE check")[1].split("5. Then remove")[0]
    assert "git switch" in sop  # the prohibition itself
    assert sop.replace("never `git switch` in the primary", "").count(
        "git switch"
    ) == 0


def test_degraded_note_sweeps_and_resolves():
    note = loop._selected_branch_note_degraded("bigtest", 7, "non-repo")
    # Degraded runs execute near the primary, so the sweep applies to
    # the worktree holding the selected branch, and the plumbing
    # landing resolves conflicts the same way.
    assert "wip/" in note
    assert "run branch's side wins" in note
    assert "--merge-base=" in note
    assert "CHERRY_PICK_HEAD" in note


def test_residue_protocol_surfaces_wip_branches():
    """ADR-0015 consequence: a wip branch is residue - surfaced in the
    shared protocol, never silently removed."""
    residue = loop._residue_protocol(7)
    assert "`wip/` branch" in residue
    assert "never silently" in residue


def test_landing_ask_option1_mentions_wip_sweep_on_master_pin():
    note = loop._selected_branch_note("master", 7, detached=True)
    # The ask's option 1 discloses the WIP fate: swept to a wip
    # branch, conflicts resolved - not a refusal.
    assert "Land in `master`" in note
    assert "wip" in note.lower()


def test_wip_branch_helper_matches_run_branch_shape():
    # Same slug discipline as the run branch (#312), wip/ prefix.
    assert loop._wip_branch_name("My Fancy Topic", 42) == "wip/my-fancy-topic-42"
    assert loop._wip_branch_name(None, 42) == "wip/chat-42"
    assert loop._wip_branch_name("new-task", 42) == "wip/chat-42"


@pytest.mark.asyncio
async def test_run_system_prompt_carries_resolution_contract(tmp_path, monkeypatch):
    """The assembled interactive system prompt carries the ADR-0015
    contract, not just the note-level functions."""
    from backend.db.database import create_conversation, update_conversation

    monkeypatch.setattr(loop, "_agents_notes", lambda workspace: "")
    cid = await create_conversation("t")
    await update_conversation(cid, selected_branch="master")

    captured = {}

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        captured["system"] = messages[0]["content"]

        async def _stream():
            yield {"type": "content", "text": "done"}
            yield {"type": "finish"}

        return _stream()

    orig_chat = loop.model_client.chat
    loop.model_client.chat = fake_chat
    try:
        async for _ in loop.run_agent(cid, "go", str(tmp_path)):
            pass
    finally:
        loop.model_client.chat = orig_chat

    system = captured["system"]
    assert "wip/" in system
    assert "run branch's side wins" in system
