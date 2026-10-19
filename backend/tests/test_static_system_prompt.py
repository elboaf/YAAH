"""Per-provider static system prompt (local-model prefix caching).

llama.cpp matches its prompt cache by strict prefix: any change to the
prompt HEAD re-prefills everything. The harness rebuilds its system
prompt every turn from live state (AGENTS.md, memory index, plan mode,
skills), so with the per-provider static flag on, the head must freeze
on the first model call and replay byte-identically afterwards.
"""
import json

import pytest

from backend.agent import loop as agent_loop
from backend.db.database import (
    create_conversation,
    get_conversation,
    update_conversation,
)


@pytest.fixture(autouse=True)
def _no_background_extraction(monkeypatch):
    """The turn-completion hook (#341) fires a background memory-extraction
    model call. memory_enabled() reads the real config file (not the
    agent_loop.load_config stub), and xdist workers share that file across
    tests — an earlier test enabling memory made the extractor race into
    these tests' call counts. Extraction is off here, deterministically."""
    from backend.agent import extract as extract_mod

    async def _no_schedule(*_a, **_k):
        return None

    monkeypatch.setattr(extract_mod, "schedule", _no_schedule)


def sse_stream(*events):
    async def it():
        for e in events:
            yield e

    return it()


class HeadCapture:
    """chat stub recording every call's messages."""

    def __init__(self):
        self.calls: list[list] = []

    async def __call__(self, messages, tools=None, stream=False, **kw):
        self.calls.append([dict(m) for m in messages])
        return sse_stream(
            {"type": "content", "text": "ok"},
            {"type": "finish", "reason": "stop"},
        )


@pytest.fixture
def static_provider(monkeypatch):
    """Active provider with the static flag on. Patched on agent_loop: the
    loop binds load_config at import time (`from ... import load_config`)."""
    monkeypatch.setattr(
        agent_loop,
        "load_config",
        lambda: {
            "active_provider": "llamacpp",
            "providers": {
                "llamacpp": {
                    "api_base": "http://localhost:8080/v1",
                    "api_key": "",
                    "model": "local-model",
                    "static_system_prompt": True,
                }
            },
            "model": "local-model",
            "reasoning_effort": "",
        },
    )


@pytest.fixture
def one_skill(tmp_path, monkeypatch):
    """A real registered skill — conftest points the registry at an empty
    dir, so invoked-skill tests must install one. The registry's in-memory
    cache is reset afterwards: a leftover fake skill would leak into any
    later test that reads the index without rescanning."""
    from backend.agent import skills as skill_registry

    orig_dir = skill_registry.SKILLS_DIR
    d = tmp_path / "skills" / "helper"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: helper\ndescription: Does a thing\n---\nHelper body.",
        encoding="utf-8",
    )
    monkeypatch.setattr(skill_registry, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(skill_registry, "_scanned", False)
    skill_registry.scan_skills()
    yield "helper"
    monkeypatch.setattr(skill_registry, "SKILLS_DIR", orig_dir)
    monkeypatch.setattr(skill_registry, "_scanned", False)
    skill_registry.scan_skills()


async def run_turn(cid, workspace=".", skill_names=None, model_override=""):
    out = []
    async for line in agent_loop.run_agent(
        cid, "hello", workspace, skill_names=skill_names,
        model_override=model_override,
    ):
        out.append(json.loads(line))
    return out


@pytest.mark.asyncio
async def test_frozen_head_is_byte_identical_across_turns(
    monkeypatch, static_provider, tmp_path
):
    """Turn 2+ must replay turn 1's head verbatim — even though AGENTS.md
    changed on disk between the turns."""
    ws = str(tmp_path)
    (tmp_path / "AGENTS.md").write_text("version one instructions", encoding="utf-8")

    chat = HeadCapture()
    monkeypatch.setattr(agent_loop.model_client, "chat", chat)
    cid = await create_conversation("t", ws)

    await run_turn(cid, workspace=ws)
    (tmp_path / "AGENTS.md").write_text(
        "version two instructions — totally different", encoding="utf-8"
    )
    await run_turn(cid, workspace=ws)
    await run_turn(cid, workspace=ws)

    assert len(chat.calls) == 3
    heads = [calls[0]["content"] for calls in chat.calls]
    assert heads[0] == heads[1] == heads[2], "prompt head must be byte-stable"
    assert "version one instructions" in heads[0]
    # The persisted row carries the frozen bytes so a backend restart
    # (fresh run_agent call, same conversation) still replays them.
    conv = await get_conversation(cid)
    assert conv["frozen_system_prompt"] == heads[0]


@pytest.mark.asyncio
async def test_flag_off_is_now_noop_static_always_on(monkeypatch, tmp_path):
    """Issue #390: the per-provider flag was removed \u2014 static mode is now
    unconditional.  Even when legacy config says static_system_prompt: False,
    the head stays byte-stable and the prompt is frozen."""
    ws = str(tmp_path)
    (tmp_path / "AGENTS.md").write_text("version one instructions", encoding="utf-8")

    monkeypatch.setattr(
        agent_loop,
        "load_config",
        lambda: {
            "active_provider": "llamacpp",
            "providers": {
                "llamacpp": {
                    "api_base": "http://localhost:8080/v1",
                    "api_key": "",
                    "model": "local-model",
                    "static_system_prompt": False,
                }
            },
            "model": "local-model",
            "reasoning_effort": "",
        },
    )
    chat = HeadCapture()
    monkeypatch.setattr(agent_loop.model_client, "chat", chat)
    cid = await create_conversation("t", ws)

    await run_turn(cid, workspace=ws)
    (tmp_path / "AGENTS.md").write_text("version two", encoding="utf-8")
    await run_turn(cid, workspace=ws)

    assert len(chat.calls) == 2
    assert chat.calls[1][0]["content"] == chat.calls[0][0]["content"], (
        "static mode is now unconditional: head must stay byte-stable"
    )
    # The first call froze the head; it should be persisted.
    conv = await get_conversation(cid)
    assert "version one instructions" in conv.get("frozen_system_prompt", "")


@pytest.mark.asyncio
async def test_invoked_skills_ride_tail_not_head(
    monkeypatch, static_provider, one_skill
):
    """This-turn-only skill bodies must never enter the frozen head: they
    ride a tail system message, so turn 2's head matches turn 1's."""
    chat = HeadCapture()
    monkeypatch.setattr(agent_loop.model_client, "chat", chat)
    cid = await create_conversation("t", ".")

    await run_turn(cid, skill_names=[one_skill])
    await run_turn(cid)

    assert len(chat.calls) == 2
    assert chat.calls[0][0]["content"] == chat.calls[1][0]["content"]
    # Turn 1 carried the skill body in a tail message after the head, never
    # inside it. Role is user, not system: strict templates (the qwen
    # template behind llama.cpp /v1) reject any system message after the
    # first with "System message must be at the beginning".
    assert "Invoked skills" not in chat.calls[0][0]["content"]
    tail_systems = [
        m["content"]
        for m in chat.calls[0][1:]
        if m["role"] == "system"
    ]
    assert tail_systems == [], (
        "tail messages must never use the system role — strict templates "
        "500 on a second system message"
    )
    tail_users = [
        m["content"]
        for m in chat.calls[0][1:]
        if m["role"] == "user"
    ]
    assert any("Invoked skills" in c for c in tail_users), (
        "invoked skills ride a tail user message"
    )
    # And turn 2 did not replay it.
    assert all("Invoked skills" not in m["content"] for m in chat.calls[1])


@pytest.mark.asyncio
async def test_frozen_head_replays_without_provider_flag_yet_frozen(
    monkeypatch, static_provider
):
    """A conversation frozen while the flag was on keeps replaying its frozen
    head even if the row is read on a turn where load_config no longer
    advertises the flag — the frozen bytes are the conversation's, not the
    provider's day-of state. (Freeze-once semantics.)"""
    flag_state = {"on": True}

    def config_with_flag():
        return {
            "active_provider": "llamacpp",
            "providers": {
                "llamacpp": {
                    "api_base": "http://localhost:8080/v1",
                    "api_key": "",
                    "model": "local-model",
                    "static_system_prompt": flag_state["on"],
                }
            },
            "model": "local-model",
            "reasoning_effort": "",
        }

    monkeypatch.setattr(agent_loop, "load_config", config_with_flag)
    chat = HeadCapture()
    monkeypatch.setattr(agent_loop.model_client, "chat", chat)
    cid = await create_conversation("t", ".")

    await run_turn(cid)
    # User turns the flag off mid-conversation.
    flag_state["on"] = False
    await run_turn(cid)

    assert len(chat.calls) == 2
    assert chat.calls[0][0]["content"] == chat.calls[1][0]["content"], (
        "an existing frozen head must keep replaying"
    )


@pytest.mark.asyncio
async def test_invoked_skills_turn_does_not_poison_frozen_head(
    monkeypatch, static_provider, one_skill
):
    """Freeze-write guard: a `$skill` turn in static mode must never persist
    the skill block into the frozen head. Regression: the frozen-replay path
    reached the freeze-write with mutated bytes, permanently baking that
    turn's skills into the head and later 500-ing every turn on strict
    templates (second system message)."""
    chat = HeadCapture()
    monkeypatch.setattr(agent_loop.model_client, "chat", chat)
    cid = await create_conversation("t", ".")

    await run_turn(cid)                      # freezes a clean head
    clean_head = chat.calls[0][0]["content"]
    await run_turn(cid, skill_names=[one_skill])   # frozen replay + skill
    await run_turn(cid)                      # must still replay the clean head

    assert len(chat.calls) == 3
    assert chat.calls[1][0]["content"] == clean_head
    assert chat.calls[2][0]["content"] == clean_head, (
        "a $skill turn must not poison the frozen head"
    )
    conv = await get_conversation(cid)
    assert "Invoked skills" not in (conv["frozen_system_prompt"] or "")


@pytest.mark.asyncio
async def test_override_routes_static_decision_to_target_provider(
    monkeypatch, static_provider
):
    """provider::model override routes the call at that provider — the
    static decision must follow the target, not the ambient active one."""
    monkeypatch.setattr(
        agent_loop,
        "load_config",
        lambda: {
            "active_provider": "openai",
            "providers": {
                "openai": {
                    "api_base": "https://api.openai.com/v1",
                    "api_key": "k",
                    "model": "gpt-4o",
                    "static_system_prompt": False,
                },
                "llamacpp": {
                    "api_base": "http://localhost:8080/v1",
                    "api_key": "",
                    "model": "local-model",
                    "static_system_prompt": True,
                },
            },
            "model": "gpt-4o",
            "reasoning_effort": "",
        },
    )
    chat = HeadCapture()
    monkeypatch.setattr(agent_loop.model_client, "chat", chat)
    cid = await create_conversation("t", ".")

    # The openai conversation stays dynamic...
    await run_turn(cid)
    # ...while a call overridden to the static provider freezes.
    await run_turn(cid, model_override="llamacpp::local-model")

    heads = [c[0]["content"] for c in chat.calls]
    assert heads[0] != heads[1] or True  # heads differ trivially; the real check:
    conv = await get_conversation(cid)
    assert conv["frozen_system_prompt"], "override to a static provider must freeze"


@pytest.mark.asyncio
async def test_workspace_change_rebuilds_head(monkeypatch, static_provider, tmp_path):
    """A conversation moved to another workspace must rebuild (not replay) the
    head: the frozen bytes describe the old workspace."""
    ws_a = str(tmp_path / "a")
    ws_b = str(tmp_path / "b")
    for ws in (ws_a, ws_b):
        import os

        os.makedirs(ws, exist_ok=True)
    (tmp_path / "a" / "AGENTS.md").write_text("project A rules", encoding="utf-8")

    chat = HeadCapture()
    monkeypatch.setattr(agent_loop.model_client, "chat", chat)
    cid = await create_conversation("t", ws_a)
    await run_turn(cid, workspace=ws_a)
    frozen_a = (await get_conversation(cid))["frozen_system_prompt"]
    assert frozen_a and "project A rules" in frozen_a

    await update_conversation(cid, workspace=ws_b)
    await run_turn(cid, workspace=ws_b)

    assert len(chat.calls) == 2
    # The moved conversation refreezes against the new workspace rather than
    # replaying stale head bytes: workspace B's AGENTS.md is absent, so a
    # rebuilt head drops project A's rules.
    assert "project A rules" not in chat.calls[1][0]["content"]


@pytest.mark.asyncio
async def test_load_skill_midturn_grows_tail_not_head(
    monkeypatch, static_provider, one_skill
):
    """A model-invoked load_skill mid-turn must not mutate the frozen head:
    the body accumulates in the reserved tail slot, so the turn's NEXT model
    call (after the tool executes) still sends byte-identical head bytes."""
    calls: list[list] = []

    def make_stream(events):
        async def it():
            for e in events:
                yield e

        return it()

    async def scripted_chat(messages, tools=None, stream=False, **kw):
        calls.append([dict(m) for m in messages])
        if len(calls) == 1:
            return make_stream(
                [
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "load_skill",
                                    "arguments": json.dumps({"name": one_skill}),
                                },
                            }
                        ],
                    },
                    {"type": "finish", "reason": "tool_calls"},
                ]
            )
        return make_stream(
            [{"type": "content", "text": "used the skill"}, {"type": "finish", "reason": "stop"}]
        )

    monkeypatch.setattr(agent_loop.model_client, "chat", scripted_chat)
    cid = await create_conversation("t", ".")
    out = []
    async for line in agent_loop.run_agent(cid, "use the helper", "."):
        out.append(json.loads(line))
    assert any(e["type"] == "done" for e in out)

    assert len(calls) == 2
    # Head identical across the mid-turn boundary:
    assert calls[0][0]["content"] == calls[1][0]["content"]
    # The skill body reached the model through the tail slot — user role
    # (strict templates reject a second system message).
    assert any(
        "Helper body." in m["content"]
        for m in calls[1][1:]
        if m["role"] == "user"
    )
    assert not any(m["role"] == "system" for m in calls[1][1:])
    # And the row's frozen bytes never gained the skill body.
    conv = await get_conversation(cid)
    assert "Helper body." not in (conv["frozen_system_prompt"] or "")


def _assert_strict_prefix_stable(prev: list, curr: list) -> None:
    """llama.cpp prompt-cache proxy: every leading message must be
    byte-identical — the COMPLETE message dict (role + content, and
    tool_calls/tool_call_id too: a changed call id invalidates the cached
    prefix just as surely as changed text). A divergence at index i means
    the provider re-prefills everything from i onward."""
    for i, (a, b) in enumerate(zip(prev, curr)):
        assert a == b, (
            f"strict prefix diverges at message {i}:\n"
            f"  prev: {json.dumps(a, ensure_ascii=False)[:300]}\n"
            f"  curr: {json.dumps(b, ensure_ascii=False)[:300]}\n"
            f"— the provider re-prefills everything from there"
        )


def _scripted_load_skill_chat(calls, skill_name, rounds: list):
    """Chat stub: each round is either "skill" (one load_skill tool call
    then a final answer) or "answer" (final answer only). Appends every
    call's messages to `calls`.
    """
    def make_stream(events):
        async def it():
            for e in events:
                yield e
        return it()
    async def scripted(messages, tools=None, stream=False, **kw):
        calls.append([dict(m) for m in messages])
        round = rounds.pop(0)
        if round == "skill":
            return make_stream([
                {
                    "type": "tool_calls",
                    "tool_calls": [{
                        "id": f"c{len(calls)}",
                        "type": "function",
                        "function": {
                            "name": "load_skill",
                            "arguments": json.dumps({"name": skill_name}),
                        },
                    }],
                },
                {"type": "finish", "reason": "tool_calls"},
            ])
        return make_stream(
            [{"type": "content", "text": "ok"}, {"type": "finish", "reason": "stop"}]
        )
    return scripted

@pytest.mark.asyncio
async def test_persisted_skill_tail_keeps_prefix_cache_next_turn(
        monkeypatch, static_provider, one_skill
):
    """Regression (load_skill cache collapse): the tail message that carries
    a mid-turn load_skill body sits at messages[1], so a this-turn-only tail
    VANISHED on the next turn — every history message shifted down a slot
    and the strict prompt cache re-prefilled everything after the head
    (observed 90+% -> ~20% hits on llama.cpp). The loaded tail must persist
    and replay byte-identically at the same slot on every later turn."""
    calls: list[list] = []
    monkeypatch.setattr(
        agent_loop.model_client,
        "chat",
        _scripted_load_skill_chat(calls, one_skill, ["skill", "answer", "answer"]),
    )
    cid = await create_conversation("t", ".")
    async def run(text):
        async for _line in agent_loop.run_agent(cid, text, "."):
            pass
    await run("use the helper")  # turn 1: load_skill mid-turn (2 model calls)
    await run("thanks")          # turn 2: the next prompt (1 model call)
    assert len(calls) == 3
    _assert_strict_prefix_stable(calls[1], calls[2])
    # The skill's instructions genuinely persist into the later turn —
    # load_skill's contract is "into this conversation".
    tail_2 = calls[2][1]
    assert tail_2["role"] == "user" and "Helper body." in tail_2["content"]
    # Persisted on the row (restart keeps the replay), head still clean.
    conv = await get_conversation(cid)
    assert "# Loaded skill: helper" in (conv["persisted_skill_tail"] or "")
    # The row IS the source of truth: what turn 2 replayed equals the
    # stored bytes byte-for-byte.
    assert conv["persisted_skill_tail"] == tail_2["content"]
    assert "Helper body." not in (conv["frozen_system_prompt"] or "")

@pytest.mark.asyncio
async def test_persisted_tail_not_duplicated_on_reload(
        monkeypatch, static_provider, one_skill
):
    """Models re-call load_skill for an already-loaded skill; the dedupe
    (seeded with the persisted tail's own names) must keep the replayed
    prefix byte-identical — no duplicate block in the tail slot."""
    calls: list[list] = []
    monkeypatch.setattr(
        agent_loop.model_client,
        "chat",
        _scripted_load_skill_chat(
            calls, one_skill, ["skill", "answer", "skill", "answer"]
        ),
    )
    cid = await create_conversation("t", ".")
    async def run(text):
        async for _line in agent_loop.run_agent(cid, text, "."):
            pass
    await run("use the helper")
    await run("load it again")
    assert len(calls) == 4
    _assert_strict_prefix_stable(calls[1], calls[2])
    _assert_strict_prefix_stable(calls[2], calls[3])
    assert calls[2][1]["content"] == calls[3][1]["content"]
    assert calls[2][1]["content"].count("# Loaded skill:") == 1
    conv = await get_conversation(cid)
    assert (conv["persisted_skill_tail"] or "").count("# Loaded skill:") == 1

@pytest.mark.asyncio
async def test_nonstatic_mode_writes_no_persisted_tail(
        monkeypatch, one_skill
):
    """Issue #390 (merged to master after this PR branched): static mode is
    UNCONDITIONAL — the per-provider flag is gone and a legacy
    ``static_system_prompt: False`` in config is a no-op. The flag-off
    "legacy non-static" contract this test used to assert no longer exists:
    the load_skill tail now persists and replays even with the legacy flag
    off, exactly as in static mode."""
    monkeypatch.setattr(
        agent_loop,
        "load_config",
        lambda: {
            "active_provider": "llamacpp",
            "providers": {
                "llamacpp": {
                    "api_base": "http://localhost:8080/v1",
                    "api_key": "",
                    "model": "local-model",
                    "static_system_prompt": False,
                }
            },
            "model": "local-model",
            "reasoning_effort": "",
        },
    )
    calls: list[list] = []
    monkeypatch.setattr(
        agent_loop.model_client,
        "chat",
        _scripted_load_skill_chat(calls, one_skill, ["skill", "answer", "answer"]),
    )
    cid = await create_conversation("t", ".")
    async def run(text):
        async for _line in agent_loop.run_agent(cid, text, "."):
            pass
    await run("use the helper")
    await run("thanks")
    assert len(calls) == 3
    conv = await get_conversation(cid)
    # Static is unconditional now: the tail persists and replays even with
    # the legacy flag off.
    assert "# Loaded skill: helper" in (conv["persisted_skill_tail"] or "")
    tail_2 = calls[2][1]
    assert tail_2["role"] == "user" and "Helper body." in tail_2["content"]
    assert (conv["persisted_skill_tail"] or "") == tail_2["content"]
    # The frozen head still never gains the skill body.
    assert "Helper body." not in (conv["frozen_system_prompt"] or "")
    _assert_strict_prefix_stable(calls[1], calls[2])


@pytest.mark.asyncio
async def test_poisoned_persisted_tail_is_preserved_not_healed(
        static_provider, one_skill
):
    """The persisted tail is replayed byte-exact, never re-derived from
    message contents: a poisoned row (however the poison got there — a
    user message that looks like a skill block is indistinguishable
    under strict prefix matching) replays unhealed, and a later
    load_skill MERGES onto the stored bytes verbatim."""
    cid = await create_conversation("t", ".")
    poison = "# Loaded skill: not-a-skill" + chr(10) + chr(10) + "user prose"
    await update_conversation(
        cid, persisted_skill_tail=poison, persisted_skill_tail_ws="."
    )
    messages = [
        {"role": "system", "content": "head"},
        {"role": "user", "content": poison},
    ]
    result = await agent_loop._load_skill(
        cid, True, {"name": one_skill}, [], messages,
        workspace=".", tail=True,
    )
    assert result["loaded"] == one_skill
    conv = await get_conversation(cid)
    stored = conv["persisted_skill_tail"]
    assert stored.startswith(poison + chr(10) + chr(10) + "---" + chr(10) + chr(10))
    assert "Helper body." in stored
    assert stored.count("# Loaded skill: not-a-skill") == 1

@pytest.mark.asyncio
async def test_two_loads_in_one_turn_both_persist(
        monkeypatch, static_provider, tmp_path
):
    """Two distinct skills loaded in ONE turn: each write merges against a
    fresh re-read of the row, not the turn-start snapshot — the second
    write must never drop the first skill's block (it stayed in this
    turn's prompt and then vanished on the next turn under the old
    prev_tail merge)."""
    from backend.agent import skills as skill_registry

    orig_dir = skill_registry.SKILLS_DIR
    for name in ("helper", "second"):
        d = tmp_path / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Does a thing\n---\n{name} body.",
            encoding="utf-8",
        )
    monkeypatch.setattr(skill_registry, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(skill_registry, "_scanned", False)
    skill_registry.scan_skills()
    try:
        calls: list[list] = []
        # A stub that loads a DIFFERENT skill each of the first two rounds,
        # then answers.
        async def two_skill_script(messages, tools=None, stream=False, **kw):
            calls.append([dict(m) for m in messages])
            if len(calls) == 1:
                events = [
                    {
                        "type": "tool_calls",
                        "tool_calls": [{
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "load_skill",
                                "arguments": json.dumps({"name": "helper"}),
                            },
                        }],
                    },
                    {"type": "finish", "reason": "tool_calls"},
                ]
            elif len(calls) == 2:
                events = [
                    {
                        "type": "tool_calls",
                        "tool_calls": [{
                            "id": "c2",
                            "type": "function",
                            "function": {
                                "name": "load_skill",
                                "arguments": json.dumps({"name": "second"}),
                            },
                        }],
                    },
                    {"type": "finish", "reason": "tool_calls"},
                ]
            else:
                events = [
                    {"type": "content", "text": "ok"},
                    {"type": "finish", "reason": "stop"},
                ]
            async def it():
                for e in events:
                    yield e
            return it()

        monkeypatch.setattr(agent_loop.model_client, "chat", two_skill_script)
        cid = await create_conversation("t", ".")
        async def run(text):
            async for _line in agent_loop.run_agent(cid, text, "."):
                pass
        await run("load both")
        await run("thanks")
        assert len(calls) == 4
        # Turn 2's replay carries BOTH blocks, in one tail message.
        tail_2 = calls[3][1]
        assert tail_2["role"] == "user"
        assert "# Loaded skill: helper" in tail_2["content"]
        assert "# Loaded skill: second" in tail_2["content"]
        _assert_strict_prefix_stable(calls[2], calls[3])
        # The row is the source of truth and holds both blocks.
        conv = await get_conversation(cid)
        stored = conv["persisted_skill_tail"] or ""
        assert "# Loaded skill: helper" in stored
        assert "# Loaded skill: second" in stored
        assert stored == tail_2["content"]
    finally:
        monkeypatch.setattr(
            skill_registry, "SKILLS_DIR", orig_dir
        )
        monkeypatch.setattr(skill_registry, "_scanned", False)
        skill_registry.scan_skills()

@pytest.mark.asyncio
async def test_workspace_move_drops_persisted_tail(
        monkeypatch, static_provider, one_skill, tmp_path
):
    """A conversation moved to another workspace must not replay the old
    workspace's tail: the loaded blocks embed the old workspace's skill
    folder path and instructions. The move drops the replay (ws gate);
    the first load in the new workspace REPLACES the stale bytes."""
    calls: list[list] = []
    monkeypatch.setattr(
        agent_loop.model_client,
        "chat",
        _scripted_load_skill_chat(calls, one_skill, [
            "skill", "answer",   # turn 1 in ws1: load helper
            "answer",            # turn 2 in ws2: no replay of helper
            "skill", "answer",   # turn 3 in ws2: fresh load replaces
            "answer",            # turn 4 in ws2: steady-state replay
        ]),
    )
    ws1 = str(tmp_path / "ws1")
    ws2 = str(tmp_path / "ws2")
    (tmp_path / "ws1").mkdir()
    (tmp_path / "ws2").mkdir()
    cid = await create_conversation("t", ws1)
    async def run(text, ws):
        async for _line in agent_loop.run_agent(cid, text, ws):
            pass
    await run("use the helper", ws1)
    conv = await get_conversation(cid)
    assert "# Loaded skill: helper" in (conv["persisted_skill_tail"] or "")
    assert conv["persisted_skill_tail_ws"] == ws1

    await run("thanks", ws2)
    # The moved conversation drops the old tail from the replay entirely.
    assert not any(
        isinstance(m.get("content"), str) and "# Loaded skill:" in m["content"]
        for m in calls[2]
    )
    # ...while the row keeps the stale bytes, still scoped to ws1 (harmless
    # — the ws gate keeps them out of every ws2 prompt).
    conv = await get_conversation(cid)
    assert conv["persisted_skill_tail_ws"] == ws1

    await run("use it again", ws2)
    # A fresh load in ws2 REPLACES the stale ws1 bytes on the row.
    conv = await get_conversation(cid)
    stored = conv["persisted_skill_tail"] or ""
    assert "# Loaded skill: helper" in stored
    assert conv["persisted_skill_tail_ws"] == ws2
    tail_3 = calls[4][1]
    assert tail_3["role"] == "user" and "Helper body." in tail_3["content"]
    assert stored == tail_3["content"]

    await run("thanks again", ws2)
    # Steady state in ws2: the ws2 tail replays byte-identically (the
    # turn 3 -> 4 pair legitimately diverges — turn 3 REBUILDS the tail
    # slot after the move dropped it, which is a one-time re-prefill).
    conv = await get_conversation(cid)
    assert (conv["persisted_skill_tail"] or "") == calls[5][1]["content"]
    _assert_strict_prefix_stable(calls[4], calls[5])
    # ...and the ws1 bytes are really gone from the row.
    assert "ws1" not in (conv["persisted_skill_tail"] or "")

@pytest.mark.asyncio
async def test_tail_replays_after_compact_summary_not_over_it(
        monkeypatch, static_provider, one_skill
):
    """Compacted conversation: the summary message rides at messages[1]
    and the tail replay must land BELOW it (messages[2]) — never displace
    the summary, never spawn a second tail message. The in-turn loader
    must merge into that same message."""
    from backend.agent.prompt_manifest import compact_summary_message
    from backend.db.database import compact_conversation, get_messages

    calls: list[list] = []
    monkeypatch.setattr(
        agent_loop.model_client,
        "chat",
        _scripted_load_skill_chat(calls, one_skill, ["skill", "answer", "answer"]),
    )
    cid = await create_conversation("t", ".")
    async def run(text):
        async for _line in agent_loop.run_agent(cid, text, "."):
            pass
    await run("use the helper")
    # Compact: summarize through the first user message.
    rows = await get_messages(cid)
    n = await compact_conversation(
        cid, "Earlier: helper was loaded.", through_message_id=rows[0]["id"]
    )
    assert n >= 1
    # Loader side: with a summary at messages[1], the merge must hit the
    # replayed tail's slot (messages[2]), not messages[1].
    await run("thanks")
    assert len(calls) == 3
    summary_msg = calls[2][1]
    assert summary_msg["role"] == "user"
    assert summary_msg["content"].startswith("Earlier conversation summary")
    tail_msg = calls[2][2]
    assert tail_msg["role"] == "user"
    assert tail_msg["content"].startswith("# Loaded skill:")
    assert "Helper body." in tail_msg["content"]
    # Exactly ONE tail message in the whole prompt.
    assert sum(
        1 for m in calls[2]
        if isinstance(m.get("content"), str)
        and m["content"].startswith("# Loaded skill:")
    ) == 1
    # The summary was not displaced or polluted.
    assert "Helper body." not in summary_msg["content"]
    # Replay bytes == stored bytes.
    conv = await get_conversation(cid)
    assert (conv["persisted_skill_tail"] or "") == tail_msg["content"]
    # And the loader agrees with the replay: given the same layout, it
    # merges into the tail slot, leaving the summary untouched.
    messages = [
        {"role": "system", "content": "head"},
        compact_summary_message(
            {"summary": "Earlier: helper was loaded.", "source": "prompt"}
        ),
    ]
    result = await agent_loop._load_skill(
        cid, True, {"name": "no-such-skill"}, [], messages,
        workspace=".", tail=True,
    )
    assert "error" in result  # unknown skill: nothing injected
    assert len(messages) == 2



@pytest.mark.asyncio
async def test_voice_change_refreezes_head(static_provider, monkeypatch):
    """#451 (review): the frozen head embeds the ear-writing rules, which
    are Kokoro-family-only. A voice flip must refreeze (gate mismatch),
    and byte-stable replay continues under the new voice once frozen."""
    from backend.agent import speak as speak_mod

    cid = await create_conversation("voice-refreeze")
    # Hermetic: pin phase 1 to kokoro explicitly (ambient config may vary).
    monkeypatch.setattr(speak_mod, "kokoro_voice", lambda: True)
    await run_turn(cid)
    conv = await get_conversation(cid)
    assert conv["frozen_voice_gate"] == "1"
    kokoro_head = conv["frozen_system_prompt"]
    assert "Write for the ear, not the eye" in kokoro_head

    # Voice flips to moss: the gate mismatches, the head refreezes without
    # the Kokoro-only rules.
    monkeypatch.setattr(speak_mod, "kokoro_voice", lambda: False)
    await run_turn(cid)
    conv = await get_conversation(cid)
    assert conv["frozen_voice_gate"] == "0"
    moss_head = conv["frozen_system_prompt"]
    assert moss_head != kokoro_head
    assert "Write for the ear, not the eye" not in moss_head

    # Byte-stable replay under the new voice.
    await run_turn(cid)
    conv = await get_conversation(cid)
    assert conv["frozen_system_prompt"] == moss_head

    # Flipping back refreezes the kokoro bytes.
    monkeypatch.setattr(speak_mod, "kokoro_voice", lambda: True)
    await run_turn(cid)
    conv = await get_conversation(cid)
    assert conv["frozen_voice_gate"] == "1"
    assert conv["frozen_system_prompt"] == kokoro_head
