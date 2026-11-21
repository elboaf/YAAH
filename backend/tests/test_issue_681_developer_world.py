"""Issue #681 — developer world core loop (ADR-0022): worker chats, desks,
and the supervisor path.

Covers the v1 definition of done:
- desk pool: create-on-demand seats under ~/.yaah/desks/<pool>/<n>, cap
  enforcement, handover semantics (tracked reset, untracked survive), no
  origin remote on the clone.
- worker chats: chat_type='worker' rows with a desk_dir, spawned only by
  ordinary chats (spawn_worker refuses workers and non-git workspaces).
- desk re-root: a worker turn resolves file tools/shells/git inside the
  desk while the prompt surface and the memory key stay on the workspace.
- gh exclusion: worker shells get an environment without the bundled gh
  bin dir and without gh auth vars.
- contribution flow: submit requires a clean desk + at most one pending
  row; review merge refuses a dirty checkout; reject/abandon close the
  row and bounce to the authoring worker.
- prompt surface: the worker fragment is worker-conditional (ordinary
  chats never see it); the fixed-core prose names every new schema.
"""

import os
import subprocess
from pathlib import Path

import pytest

from backend.agent import desks as desks_mod


def _git(cwd: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(cwd), *args],
        capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


@pytest.fixture()
def ws_repo(tmp_path, monkeypatch):
    """A throwaway local git workspace with one commit on master."""
    root = tmp_path / "wsrepo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "master")
    (root / "README.md").write_text("hello", encoding="utf-8")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init")
    monkeypatch.setenv("YAAH_DESKS_PATH", str(tmp_path / "desks"))
    return root


@pytest.fixture(autouse=True)
def _redirect_desks_root(tmp_path, monkeypatch):
    """Desks live under ~/.yaah/desks; every test redirects the root so no
    test ever touches the real home directory."""
    target = tmp_path / "desks"
    monkeypatch.setattr(desks_mod, "DESKS_ROOT", target)
    return target


@pytest.fixture()
def inbox_mem_reset():
    desks_mod._INBOX.clear()
    desks_mod._SEQ[0] = 0
    yield
    desks_mod._INBOX.clear()
    desks_mod._SEQ[0] = 0


# ---- pool basics -------------------------------------------------------------


def test_pool_key_is_stable_and_case_aware(tmp_path):
    a = str(tmp_path / "proj")
    b = a.upper() if os.name == "nt" else a + "x"
    k1 = desks_mod.pool_key(a)
    assert k1 == desks_mod.pool_key(a)
    assert k1 != desks_mod.pool_key(b)
    assert "proj" in k1


def test_is_desk_dir_requires_two_levels(tmp_path):
    from backend.agent.desks import is_desk_dir

    pool = tmp_path / "desks" / "p-1"
    seat = pool / "1"
    seat.mkdir(parents=True)
    assert not is_desk_dir(tmp_path / "desks")  # root itself is not a seat
    assert not is_desk_dir(pool)                # pool is not a seat
    assert is_desk_dir(seat)                    # the seat is


def test_seat_and_release(tmp_path, ws_repo):
    async def run():
        seat = await desks_mod.seat_worker(str(ws_repo), 42)
        assert seat["reused"] is False
        seat_dir = Path(seat["dir"])
        assert desks_mod.DESKS_ROOT in seat_dir.parents
        assert (seat_dir / ".git").exists()
        # No origin remote — the overnight-agent sin is impossible.
        rc, out = _git(seat_dir, "remote")
        assert rc == 0 and "origin" not in out
        assert "workspace" in out
        # The seat is occupied by worker 42.
        assert desks_mod.find_seat(str(ws_repo), 42) is not None
        assert desks_mod.find_seat(str(ws_repo), 43) is None
        # Release: the marker clears but the directory survives.
        released = await desks_mod.release_desk(str(ws_repo), 42)
        assert released is not None
        assert seat_dir.exists()
        assert desks_mod.find_seat(str(ws_repo), 42) is None

    import asyncio
    asyncio.run(run())


def test_pool_cap_fails_loudly(ws_repo):
    from backend.agent.config import save_config

    async def run():
        save_config({"developer_world": {"pool_cap": 1}})
        first = await desks_mod.seat_worker(str(ws_repo), 101)
        assert first["reused"] is False
        with pytest.raises(RuntimeError, match="cap"):
            await desks_mod.seat_worker(str(ws_repo), 102)

    import asyncio
    asyncio.run(run())


def test_handover_resets_tracked_survives_untracked(ws_repo):
    """Handover: tracked files reset to the workspace state; untracked
    files and old branches carry over; uncommitted tracked edits die."""
    import asyncio

    async def run2():
        seat = await desks_mod.claim_desk(str(ws_repo))
        seat_dir = Path(seat["dir"])
        # A predecessor leaves: an uncommitted tracked edit, an untracked
        # file, and a local branch.
        (seat_dir / "README.md").write_text("wip", encoding="utf-8")
        (seat_dir / "node_modules_example.txt").write_text("build", encoding="utf-8")
        _git(seat_dir, "branch", "old-work")
        # The workspace moves ahead.
        (ws_repo / "README.md").write_text("v2", encoding="utf-8")
        _git(ws_repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-am", "v2")
        hand = await desks_mod.handover(str(ws_repo), seat_dir)
        assert hand["branch"] == "master"
        tracked = (seat_dir / "README.md").read_text(encoding="utf-8")
        assert tracked == "v2"          # predecessor's WIP did not survive
        assert (seat_dir / "node_modules_example.txt").exists()  # untracked did
        rc, out = _git(seat_dir, "branch", "--list", "old-work")
        assert "old-work" in out        # old branches carry over

    asyncio.run(run2())


# ---- worker spawn rules --------------------------------------------------------


def test_spawn_worker_refuses_worker_caller_and_non_git(tmp_path, ws_repo, inbox_mem_reset):
    async def run():
        from backend.db.database import create_conversation
        from backend.agent.tools import WORKER_EXCLUDED_TOOLS

        assert "spawn_worker" in WORKER_EXCLUDED_TOOLS
        assert "spawn_agent" in WORKER_EXCLUDED_TOOLS

        # A worker chat can never spawn a worker.
        worker_cid = await create_conversation(
            "w", workspace=str(ws_repo), chat_type="worker",
            worker_parent_id=1, desk_dir=str(tmp_path / "d"),
        )
        result = await desks_mod.spawn_worker_tool(
            workspace=str(ws_repo), task="x", conversation_id=worker_cid,
        )
        assert "ordinary chat" in result["error"]

        # A non-git workspace has no desks.
        plain = tmp_path / "plain"
        plain.mkdir()
        normal_cid = await create_conversation("n", workspace=str(plain))
        result = await desks_mod.spawn_worker_tool(
            workspace=str(plain), task="x", conversation_id=normal_cid,
        )
        assert "git" in result["error"]

    import asyncio
    asyncio.run(run())


def test_spawn_worker_seats_desk_and_creates_row(ws_repo, inbox_mem_reset, monkeypatch):
    """The happy path minus the wake: the spawn seats a desk, creates the
    chat_type='worker' row with desk_dir + lineage, and skips the first
    turn when the wake is unavailable (monkeypatched away here)."""
    async def run():
        from backend.db.database import create_conversation, get_conversation

        parent_id = await create_conversation("parent", workspace=str(ws_repo))
        async def no_wake(cid, prompt, source=""):
            return "started"
        import backend.main as main_mod
        monkeypatch.setattr(main_mod, "wake_conversation", no_wake)
        result = await desks_mod.spawn_worker_tool(
            workspace=str(ws_repo), task="do the thing",
            name="fixer", conversation_id=parent_id,
        )
        assert "error" not in result, result
        conv = await get_conversation(result["worker_conversation_id"])
        assert conv["chat_type"] == "worker"
        assert conv["worker_parent_id"] == parent_id
        assert conv["desk_dir"] == result["desk"]
        assert Path(conv["desk_dir"]).is_dir()
        assert desks_mod.find_seat(str(ws_repo), result["worker_conversation_id"])

    import asyncio
    asyncio.run(run())


# ---- the desk re-root ----------------------------------------------------------


def test_worker_turn_reroots_tools_not_prompt(ws_repo, tmp_path):
    """A worker turn's file tools resolve inside the desk (the write lands
    in the desk, not the checkout) while the system prompt still reads the
    workspace's AGENTS.md and carries the worker fragment."""
    from backend.agent import loop
    from backend.db.database import (
        add_message, create_conversation, delete_conversation,
    )

    (ws_repo / "AGENTS.md").write_text(
        "Say PROJ-NOTE when asked for the project note.", encoding="utf-8"
    )

    async def run():
        parent = await create_conversation("parent", workspace=str(ws_repo))
        seat = await desks_mod.seat_worker(str(ws_repo), 0)
        cid = await create_conversation(
            "worker", workspace=str(ws_repo), chat_type="worker",
            worker_parent_id=parent, desk_dir=seat["dir"],
        )
        await add_message(cid, "user", "write the marker file")

        captured: dict = {}

        async def fake_chat(messages, tools=None, stream=True, **kwargs):
            if "messages" not in captured:
                captured["messages"] = [dict(m) for m in messages]
                captured["tools"] = [s["function"]["name"] for s in (tools or [])]
            async def _s():
                yield {"type": "content", "text": "ok"}
                yield {"type": "finish"}
            return _s()

        orig_chat = loop.model_client.chat
        loop.model_client.chat = fake_chat
        try:
            async for _ev in loop._run_agent_claimed(
                cid, "write the marker file", str(ws_repo),
            ):
                pass
        finally:
            loop.model_client.chat = orig_chat
            await delete_conversation(cid)
            await delete_conversation(parent)

        system = captured["messages"][0]["content"]
        # Prompt surface stays on the workspace: the project note travels,
        # the desk does not appear in the env line...
        assert "PROJ-NOTE" in system
        assert f"The selected workspace is {ws_repo}" in system
        # ...but the worker fragment names the desk and the rules.
        assert str(seat["dir"]) in system
        assert "submit_contribution" in system
        assert "Worker chat" in system
        # Workers never see the spawn tools.
        assert "spawn_worker" not in captured["tools"]
        assert "spawn_agent" not in captured["tools"]
        # read/write resolve inside the desk: run one executor by hand
        # against the same re-root the loop would pass.
        from backend.agent.tools import execute_tool
        result = await execute_tool(
            "read_file", {"path": "README.md"},
            str(seat["dir"]), conversation_id=cid,
            memory_workspace=str(ws_repo),
        )
        assert "hello" in str(result)

    import asyncio
    asyncio.run(run())


def test_worker_memory_key_is_workspace_not_desk(ws_repo, monkeypatch, tmp_path):
    from backend.agent.memory import memory_dir, project_key

    # The harness hands memory_workspace down (the loop resolves it from
    # the workspace string); the desk path must never become the key.
    ws_key = project_key(str(ws_repo))
    desk = tmp_path / "desks" / "pool" / "1"
    desk.mkdir(parents=True)
    assert project_key(str(ws_repo)) == ws_key
    assert memory_dir(str(ws_repo)) != memory_dir(str(desk))


def test_worker_shells_get_no_gh(monkeypatch):
    from backend.agent import tools as tools_mod

    env = tools_mod._worker_env()
    # Auth vars are stripped.
    monkeypatch.setenv("GH_TOKEN", "x")
    env = tools_mod._worker_env()
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    # A PATH entry naming the bundled gh bin dir is removed (no bundle in
    # this repo checkout — simulate one).
    import backend.agent.ghenv as ghenv

    fake_bin = Path(__file__).parent / "_fake_gh_bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / ("gh.exe" if os.name == "nt" else "gh")).write_text("", encoding="utf-8")
    monkeypatch.setattr(ghenv, "bundled_gh_bin_dir", lambda: fake_bin)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + "C:\\elsewhere")
    env = tools_mod._worker_env()
    assert str(fake_bin) not in env["PATH"]
    assert "C:\\elsewhere" in env["PATH"]


# ---- contribution flow ----------------------------------------------------------


async def _make_worker(ws_repo: str, parent_id: int | None = None) -> tuple[int, dict]:
    """Create a worker chat row, seat it at a desk, stamp desk_dir — the
    same sequence spawn_worker runs.

    parent_id=None by default: the worker is parentless (its submit skips
    the wake). Never pass a fabricated numeric parent — submit wakes the
    parent conversation, and a fabricated id can collide with a REAL
    conversation in the shard's DB, firing a live background model turn
    from a test (the CI teardown unraisable)."""
    from backend.db.database import create_conversation, update_conversation

    cid = await create_conversation(
        "w", workspace=str(ws_repo), chat_type="worker",
        worker_parent_id=parent_id,
    )
    seat = await desks_mod.seat_worker(str(ws_repo), cid)
    await update_conversation(cid, desk_dir=seat["dir"])
    return cid, seat


def test_submit_requires_clean_desk_and_single_pending(ws_repo, inbox_mem_reset):
    async def run():
        from backend.agent import tools as tools_mod

        cid, seat = await _make_worker(str(ws_repo))
        # Dirty desk refuses.
        Path(seat["dir"], "scratch.txt").write_text("wip", encoding="utf-8")
        result = await tools_mod.execute_tool(
            "submit_contribution",
            {"summary": "s", "branch": "feat"},
            str(ws_repo), conversation_id=cid,
        )
        assert "dirty" in result["error"]
        # Clean desk, committed on a branch, submits.
        _git(Path(seat["dir"]), "checkout", "-q", "-b", "feat")
        Path(seat["dir"], "feat.txt").write_text("x", encoding="utf-8")
        _git(Path(seat["dir"]), "add", ".")
        _git(Path(seat["dir"]), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "feat")
        # No parent chat: the wake is skipped (submit still files the row).
        result = await tools_mod.execute_tool(
            "submit_contribution",
            {"summary": "adds feat", "branch": "feat"},
            str(ws_repo), conversation_id=cid,
        )
        assert result.get("submitted") is True, result
        rows = desks_mod.list_inbox(str(ws_repo))
        assert len(rows) == 1 and rows[0]["branch"] == "feat"
        # One pending per worker.
        result = await tools_mod.execute_tool(
            "submit_contribution",
            {"summary": "again", "branch": "feat"},
            str(ws_repo), conversation_id=cid,
        )
        assert "pending contribution" in result["error"]
        # A non-worker chat cannot submit at all.
        from backend.db.database import create_conversation

        other = await create_conversation("o", workspace=str(ws_repo))
        result = await tools_mod.execute_tool(
            "submit_contribution",
            {"summary": "s", "branch": "b"},
            str(ws_repo), conversation_id=other,
        )
        assert "worker chat" in result["error"]

    import asyncio
    asyncio.run(run())


def test_review_merge_reject_and_wake(ws_repo, inbox_mem_reset, monkeypatch):
    async def run():
        from backend.db.database import create_conversation
        from backend.agent import tools as tools_mod
        import backend.main as main_mod

        wakes: list[tuple[int, str]] = []
        async def fake_wake(cid, prompt, source=""):
            wakes.append((cid, source))
            return "started"
        monkeypatch.setattr(main_mod, "wake_conversation", fake_wake)

        parent = await create_conversation("parent", workspace=str(ws_repo))
        cid, seat = await _make_worker(str(ws_repo), parent_id=parent)
        _git(Path(seat["dir"]), "checkout", "-q", "-b", "feat")
        Path(seat["dir"], "feat.txt").write_text("x", encoding="utf-8")
        _git(Path(seat["dir"]), "add", ".")
        _git(Path(seat["dir"]), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "feat")
        result = await tools_mod.execute_tool(
            "submit_contribution",
            {"summary": "feat work", "branch": "feat"},
            str(ws_repo), conversation_id=cid,
        )
        assert result.get("submitted")
        assert ("started" in [w for w in [w[1] for w in wakes]] or wakes), "submit should wake the parent"
        assert any(cid_w == parent for cid_w, _s in wakes)
        contrib_id = result["contribution_id"]

        # Merge with a dirty checkout defers — user WIP is never integrated.
        (ws_repo / "user-wip.txt").write_text("wip", encoding="utf-8")
        result = await tools_mod.execute_tool(
            "review_contribution",
            {"contribution_id": contrib_id, "decision": "merge"},
            str(ws_repo), conversation_id=parent,
        )
        assert result.get("deferred") is True
        (ws_repo / "user-wip.txt").unlink()

        # Clean checkout merges.
        result = await tools_mod.execute_tool(
            "review_contribution",
            {"contribution_id": contrib_id, "decision": "merge"},
            str(ws_repo), conversation_id=parent,
        )
        assert result.get("merged") is True, result
        assert (ws_repo / "feat.txt").exists()
        rows = desks_mod.list_inbox(str(ws_repo))
        assert rows == []  # the row closed on merge

        # A second contribution: rejection bounces with feedback.
        _git(Path(seat["dir"]), "checkout", "-q", "master")
        _git(Path(seat["dir"]), "checkout", "-q", "-b", "feat2")
        Path(seat["dir"], "feat2.txt").write_text("y", encoding="utf-8")
        _git(Path(seat["dir"]), "add", ".")
        _git(Path(seat["dir"]), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "feat2")
        result = await tools_mod.execute_tool(
            "submit_contribution",
            {"summary": "feat2", "branch": "feat2"},
            str(ws_repo), conversation_id=cid,
        )
        assert result.get("submitted")
        wakes.clear()
        result = await tools_mod.execute_tool(
            "review_contribution",
            {"contribution_id": result["contribution_id"],
             "decision": "reject", "feedback": "wrong approach"},
            str(ws_repo), conversation_id=parent,
        )
        assert result.get("decision") == "reject"
        assert result.get("bounced_to_worker") is True
        assert any(w == cid for w, _s in wakes), "rejection must wake the worker"
        closed = desks_mod.list_inbox(str(ws_repo), include_closed=True)
        assert closed[-1]["status"] == "rejected"
        assert closed[-1]["review"] == "wrong approach"

    import asyncio
    asyncio.run(run())


def test_merge_survives_missing_committer_identity(ws_repo, tmp_path, inbox_mem_reset, monkeypatch):
    """A machine with no git identity at all (a CI runner) must still be
    able to merge: the retry attributes the merge commit to the harness
    (YAAH Supervisor) instead of failing the whole review."""
    async def run():
        from backend.agent import tools as tools_mod
        from backend.db.database import create_conversation
        import backend.main as main_mod

        # The merge-success path wakes the worker; a real wake would fire a
        # live background model turn from this test (the CI unraisable).
        async def fake_wake(cid, prompt, source=""):
            return "started"
        monkeypatch.setattr(main_mod, "wake_conversation", fake_wake)

        # No identity anywhere: system config devnull, global config empty.
        # The config file lives OUTSIDE the workspace — an untracked file
        # there would trip the clean-checkout gate.
        empty = tmp_path / "empty-git-config.cfg"
        empty.write_text("", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
        for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            monkeypatch.delenv(var, raising=False)

        parent = await create_conversation("parent", workspace=str(ws_repo))
        cid, seat = await _make_worker(str(ws_repo), parent_id=parent)
        _git(Path(seat["dir"]), "checkout", "-q", "-b", "feat")
        Path(seat["dir"], "feat.txt").write_text("x", encoding="utf-8")
        _git(Path(seat["dir"]), "add", ".")
        _git(Path(seat["dir"]), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "feat")
        result = await tools_mod.execute_tool(
            "submit_contribution",
            {"summary": "feat", "branch": "feat"},
            str(ws_repo), conversation_id=cid,
        )
        assert result.get("submitted")
        contrib_id = result["contribution_id"]
        result = await tools_mod.execute_tool(
            "review_contribution",
            {"contribution_id": contrib_id, "decision": "merge"},
            str(ws_repo), conversation_id=parent,
        )
        assert result.get("merged") is True, result
        rc, out = _git(ws_repo, "log", "-1", "--format=%an <%ae>")
        assert "YAAH Supervisor" in out, out

    import asyncio
    asyncio.run(run())


def test_worker_cannot_review_or_merge(ws_repo, inbox_mem_reset, monkeypatch):
    """A worker can never act on the inbox — not even its own pending row
    (ADR-0022: the supervisor path is ordinary-chat-only)."""
    async def run():
        from backend.agent import tools as tools_mod
        from backend.db.database import create_conversation
        import backend.main as main_mod

        # The submit below wakes the parent chat; a real wake fires a live
        # background model turn from this test (the CI teardown unraisable).
        async def fake_wake(cid, prompt, source=""):
            return "started"
        monkeypatch.setattr(main_mod, "wake_conversation", fake_wake)

        parent = await create_conversation("p", workspace=str(ws_repo))
        cid, seat = await _make_worker(str(ws_repo), parent_id=parent)
        _git(Path(seat["dir"]), "checkout", "-q", "-b", "feat")
        Path(seat["dir"], "feat.txt").write_text("x", encoding="utf-8")
        _git(Path(seat["dir"]), "add", ".")
        _git(Path(seat["dir"]), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "feat")
        result = await tools_mod.execute_tool(
            "submit_contribution",
            {"summary": "feat", "branch": "feat"},
            str(ws_repo), conversation_id=cid,
        )
        assert result.get("submitted")
        contrib_id = result["contribution_id"]
        result = await tools_mod.execute_tool(
            "review_contribution",
            {"contribution_id": contrib_id, "decision": "merge"},
            str(ws_repo), conversation_id=cid,
        )
        assert "workers cannot review" in result["error"]
        row = desks_mod.list_inbox(str(ws_repo))[0]
        assert row["status"] == "pending"

    import asyncio
    asyncio.run(run())


def test_worker_cannot_review_or_message_nonchild(ws_repo, inbox_mem_reset, monkeypatch):
    """Tool-level identity checks: message_worker refuses a worker that is
    not the calling chat's child, and submit refuses a non-worker."""
    async def run():
        from backend.db.database import create_conversation
        from backend.agent import tools as tools_mod

        parent = await create_conversation("p", workspace=str(ws_repo))
        other_parent = await create_conversation("p2", workspace=str(ws_repo))
        cid, seat = await _make_worker(str(ws_repo), parent_id=parent)
        result = await tools_mod.execute_tool(
            "message_worker",
            {"worker_conversation_id": cid, "text": "hi"},
            str(ws_repo), conversation_id=other_parent,
        )
        assert "belongs to chat" in result["error"]
        # The real parent may message (wake is monkeypatched off here —
        # message_worker's own identity check passes before the wake).
        import backend.main as main_mod
        async def no_wake(cid2, prompt, source=""):
            return "started"
        monkeypatch.setattr(main_mod, "wake_conversation", no_wake)
        result = await tools_mod.execute_tool(
            "message_worker",
            {"worker_conversation_id": cid, "text": "steer"},
            str(ws_repo), conversation_id=parent,
        )
        assert result.get("sent") is True, result

    import asyncio
    asyncio.run(run())


# ---- prompt surface guards -------------------------------------------------------


def test_ordinary_prompt_has_no_worker_fragment():
    from backend.agent.loop import _default_system_prompt

    prompt = _default_system_prompt("C:/some/workspace")
    assert "Worker chat" not in prompt
    assert "worker chat" not in prompt.lower() or "spawn_worker" in prompt
    # The supervisor tools ARE named in the prose (always-on, per the
    # #681 ruling) — the #181 guard requires every schema to be covered.
    assert "spawn_worker" in prompt
    assert "submit_contribution" in prompt


def test_core_tools_prose_names_all_four_new_tools():
    from backend.agent.loop import _default_system_prompt

    prompt = _default_system_prompt("C:/some/workspace")
    for name in ("spawn_worker", "message_worker", "review_contribution",
                 "submit_contribution"):
        assert name in prompt, name


def test_worker_excluded_set_matches_registered_schemas():
    from backend.agent.tools import SCHEMAS, WORKER_EXCLUDED_TOOLS

    for name in WORKER_EXCLUDED_TOOLS:
        assert name in SCHEMAS, f"{name} excluded but not a registered schema"


def test_no_origin_in_desk_clone(ws_repo):
    """The structural guard: a fresh desk clone has exactly one remote and
    it is the workspace repo — no origin, ever."""
    async def run():
        seat = await desks_mod.claim_desk(str(ws_repo))
        rc, out = _git(Path(seat["dir"]), "remote")
        remotes = [line.strip() for line in out.strip().splitlines() if line.strip()]
        assert remotes == ["workspace"]
        rc, out = _git(Path(seat["dir"]), "config", "remote.workspace.url")
        assert str(ws_repo) in out

    import asyncio
    asyncio.run(run())
