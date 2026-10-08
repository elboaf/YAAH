"""Issue #349: the end-of-work landing ask.

Today a finishing agent goes quiet and waits for the user to type
"land it"/"scrap it" (#290's residue protocol). This issue inverts the
direction: the agent proactively asks via ask_user when its request
completes with unlanded residue. The ask is a PROMPT contract (no
harness machinery): the selector note carries an ask block naming the
four options (land in the selected branch / land in another branch /
leave for now / scrap it), and that block appears only where ask_user
exists - never in a sub-agent's copy of the note (sub-agents have no
ask_user). Landing into a branch checked out in the primary tree
(master pin) becomes agent-executable under the ADR-0014 safe-sync
SOP: land in plumbing first, then update-ref + reset. Since #351
(ADR-0015) a dirty primary no longer refuses the move - its WIP is
swept to a local wip branch (checkout-free) and conflicts resolve
with the run branch's side winning; only a primary mid
merge/rebase/cherry-pick still freezes.
"""

import pytest

from backend.agent import loop


def test_note_attached_carries_landing_ask():
    note = loop._selected_branch_note("bigtest", 7)
    # The ask block: proactive end-of-work question via ask_user with
    # the four settled options (issue #349).
    assert "ask_user" in note
    assert "Land in `bigtest`" in note
    assert "Land in another branch" in note
    assert "Leave for now" in note
    assert "Scrap it" in note


def test_landing_ask_names_trigger_discipline():
    note = loop._selected_branch_note("bigtest", 7)
    # Ask once, at request completion, only when there is residue to
    # surface - never mid-task, never on a nothing-changed run, and
    # never re-asking an outcome the user pre-stated.
    assert "request is complete" in note
    assert "mid-task" in note.lower()
    assert "pre-stated" in note.lower()


def test_landing_ask_other_branch_repoints_pin():
    note = loop._selected_branch_note("bigtest", 7)
    # "Land in another branch" means the chat continues there: the
    # branch_select re-point happens before the landing.
    assert "branch_select" in note


def test_detached_note_carries_master_safe_sync():
    note = loop._selected_branch_note("master", 7, detached=True)
    # The pinned branch is checked out in the primary tree: landing is
    # offered (ADR-0014), never awaited.
    assert "Land in `master`" in note
    # Safe-sync SOP: clean-check BEFORE the ref move, reset AFTER.
    assert "update-ref refs/heads/master" in note
    assert "reset --hard master" in note
    # The guarded refusal, narrowed by ADR-0015: a primary mid
    # merge/rebase/cherry-pick freezes the landing (dirty WIP is swept).
    assert "MERGE_HEAD" in note
    assert "manual-only" in note.lower()
    # The plumbing guard: branch -f cannot move a checked-out branch.
    assert "branch -f" in note
    # Diverged master lands via plumbing merge, not a checkout.
    assert "merge-tree --write-tree" in note


def test_degraded_note_carries_landing_ask():
    note = loop._selected_branch_note_degraded("bigtest", 7, "non-repo")
    assert "ask_user" in note
    assert "Land in `bigtest`" in note


def test_stale_note_has_no_landing_ask():
    note = loop._selected_branch_note_stale("bigtest", 7)
    # Nothing can land onto a dead pin; the ask asks the user to pick
    # a live branch by other means (#302 behavior stays).
    assert "Land in" not in note


def test_sub_agent_note_stripped_of_landing_ask():
    note = loop._selected_branch_note("bigtest", 7)
    stripped = loop._note_without_landing_ask(note)
    # Sub-agents have no ask_user: their copy of the note must not
    # instruct one to ask.
    assert "ask_user" not in stripped
    assert "Land in another branch" not in stripped
    # Branch context survives the strip (ADR-0010 amendment, decision 5).
    assert "# Branch selector: bigtest" in stripped
    # The passive residue protocol stays: typed "land it"/"scrap it"
    # remain the fallback in every variant.
    assert "land it" in stripped or "scrap it" in stripped
    assert loop._note_without_landing_ask("") == ""


@pytest.mark.asyncio
async def test_run_injects_landing_ask(tmp_path, monkeypatch):
    """The assembled interactive system prompt carries the ask block."""
    from backend.db.database import create_conversation, update_conversation

    monkeypatch.setattr(loop, "_agents_notes", lambda workspace: "")
    cid = await create_conversation("t")
    await update_conversation(cid, selected_branch="bigtest")

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
    assert "# Branch selector: bigtest" in system
    assert "End-of-work landing ask" in system
    assert "Land in `bigtest`" in system


def test_sub_agent_system_prompt_strips_landing_ask():
    """The strip lives at the sub-agent seam: every spawned sub-agent
    gets the parent's branch context but never the ask instruction."""
    from backend.agent.subagents import _sub_agent_system_prompt, get_agent_def, list_agents

    names = [d["name"] for d in list_agents()]
    if not names:
        pytest.skip("no agent definitions available")
    defn = get_agent_def(names[0])
    parent_note = loop._selected_branch_note("bigtest", 7)
    prompt = _sub_agent_system_prompt(
        defn, ".", branch_note=parent_note,
    )
    assert "# Branch selector: bigtest" in prompt
    assert "End-of-work landing ask" not in prompt
    assert "Land in another branch" not in prompt


def test_can_ask_landing_question_gate():
    """Same semantics as the ask_user tool itself: interactive chats
    (policy is None) can always ask; scheduled runs only with the
    allow_ask_user opt-in."""
    assert loop._can_ask_landing_question(policy=None, allow_ask_user=False)
    assert loop._can_ask_landing_question(policy=None, allow_ask_user=True)
    assert not loop._can_ask_landing_question(policy="sandbox-only", allow_ask_user=False)
    assert loop._can_ask_landing_question(policy="sandbox-only", allow_ask_user=True)


async def _capture_with_policy(tmp_path, monkeypatch, **kwargs):
    from backend.db.database import create_conversation, update_conversation

    monkeypatch.setattr(loop, "_agents_notes", lambda workspace: "")
    cid = await create_conversation("t")
    await update_conversation(cid, selected_branch="bigtest")
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
        async for _ in loop.run_agent(cid, "go", str(tmp_path), **kwargs):
            pass
    finally:
        loop.model_client.chat = orig_chat
    return captured["system"]


@pytest.mark.asyncio
async def test_scheduled_run_without_optin_gets_no_ask(tmp_path, monkeypatch):
    """Unattended run (no allow_ask_user): passive residue text only -
    the ask would instruct a question nobody can answer."""
    system = await _capture_with_policy(
        tmp_path, monkeypatch, policy="sandbox-only", allow_ask_user=False,
    )
    assert "# Branch selector: bigtest" in system
    assert "End-of-work landing ask" not in system
    assert "land it" in system or "scrap it" in system


@pytest.mark.asyncio
async def test_scheduled_run_with_optin_gets_ask(tmp_path, monkeypatch):
    system = await _capture_with_policy(
        tmp_path, monkeypatch, policy="sandbox-only", allow_ask_user=True,
    )
    assert "End-of-work landing ask" in system
    assert "Land in `bigtest`" in system
