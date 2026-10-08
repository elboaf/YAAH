"""Prompt manifest harness (issue #161).

Drives YAAH's REAL prompt-assembly entry points across the render matrix --
every configuration combination the product can produce -- and emits one
manifest per combination: ordered section list, per-section and total byte
sizes, content hashes, tool schemas, sub-agent/auxiliary prompts, and the
fully rendered text. Nothing here re-implements or hand-reconstructs prompt
text; the harness only injects at the seams the tests already use
(monkeypatched os.name, registered fixture RemoteSession, redirected
DB/config/memory/skills roots, scripted model_client.chat).

Combo id grammar (flags in canonical order):
  <win|posix>-<family>[-plan|-noskills|-nomemory|-override|-compaction|-sandboxonly]
Families:
  local        full chat turn through loop.run_agent_turn
  remote       chat turn over a remote-namespaced workspace (fixture host)
  remote-offline  remote workspace whose owning device is NOT registered
                 (the real offline note from remote_runner._system_prompt)
  subagents    built-in sub-agent prompts (general-purpose, explore)
  auxiliary    auxiliary model prompts: compaction summarizer + title gen

Non-Windows renders: the prompt builders read os.name LIVE, so the harness
flips os.name around the pure builders (test_agent.py precedent). The
Windows-only tool schemas are appended at tools.py import time, so posix
combos filter the schema list to the non-Windows shape instead.

Remote-on-one-machine decision: a REAL backend.agent.remote.RemoteSession
built from a fixture handshake info dict -- its env_line() only reads the
info dict and never connects (precedent: test_remote_workspace_target.py,
test_agent.py). No production seam was added; the fixture contract is
documented in the committed backend/prompt_manifests/README.md.

CLI:
  python -m backend.agent.prompt_manifest --list
  python -m backend.agent.prompt_manifest --render <combo-id> [--out FILE]
  python -m backend.agent.prompt_manifest --all [--out-dir backend/prompt_manifests]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Environment isolation: point every storage seam at throwaway paths BEFORE
# any backend import (backend/tests/conftest.py precedent). Run through this
# module's CLI, the user's real ~/.yaah database/config/skills/memory are
# never touched; inside pytest, conftest.py has already set these.
# ---------------------------------------------------------------------------

# The host platform gates what this process can render: win-* combos
# need the real Windows builders; posix-* renders are native on a
# posix host and flip-simulated on Windows.
HOST_WINDOWS = os.name == "nt"

_TMP_ROOT = None


def _bootstrap_env() -> Path:
    """Redirect DB/config/skills/memory/agents roots to a throwaway dir.

    Idempotent. Returns the throwaway root. Variables already set (pytest
    conftest) are left alone."""
    global _TMP_ROOT
    if os.environ.get("YAAH_DB_PATH") and os.environ.get("YAAH_CONFIG_PATH"):
        return Path(tempfile.gettempdir())
    if _TMP_ROOT is None:
        _TMP_ROOT = Path(tempfile.mkdtemp(prefix="yaah-prompt-manifest-"))
        os.environ["YAAH_DB_PATH"] = str(_TMP_ROOT / "agent.db")
        os.environ["YAAH_CONFIG_PATH"] = str(_TMP_ROOT / "config.json")
        os.environ["YAAH_SKILLS_PATH"] = str(_TMP_ROOT / "skills")
        os.environ["YAAH_MEMORY_PATH"] = str(_TMP_ROOT / "memory")
        os.environ["YAAH_AGENTS_PATH"] = str(_TMP_ROOT / "agents")
    return _TMP_ROOT


_BOOTSTRAPPED = False


_WARMED = False


def _warm_lazy_imports():
    """One throwaway unflipped turn so every lazy import (pydantic
    model validators, plugin entry-point scans, importlib.metadata
    dist caches) happens while os.name is real. pydantic caches its
    plugin scan, so later renders inside a flipped os.name never
    construct paths for importlib.metadata.
    """
    global _WARMED
    if _WARMED:
        return
    _WARMED = True
    try:
        render_local_family(
            "warmup",
            {
                "kind": "local",
                "plan": False,
                "skills": False,
                "memory": False,
                "override": False,
                "compaction": False,
                "policy": False,
            },
        )
    except Exception:  # noqa: BLE001 (warm-up is best-effort)
        pass


def _ensure_backend():
    """Import backend modules after env redirect; write a minimal config."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _bootstrap_env()
    from backend.agent import config as config_mod

    if not config_mod.CONFIG_PATH.exists():
        config_mod.save_config({"access_mode": "full"})
    _BOOTSTRAPPED = True
    _warm_lazy_imports()


# ---------------------------------------------------------------------------
# Section naming: known opening words of production sections, in output
# order (longest prefixes first -- names are matched with startswith).
# ---------------------------------------------------------------------------

SECTION_OPENINGS = [
    ("invoked-skills", "# Invoked skills"),
    ("override", "You are a pirate."),
    ("plan-mode", "# Access mode: PLAN"),
    ("sandbox-only", "# Scheduled agent: sandbox-only policy"),
    ("project-notes", "# Project notes ("),
    ("project-notes", "# Project notes"),
    ("persistent-memory", "# Persistent memory ("),
    ("persistent-memory", "# Persistent memory"),
    ("windows-sandbox", "# Windows Sandbox (for tests that need isolation)"),
    ("skills-index", "Skills available (load with the load_skill tool"),
    ("subagent-index", "Sub-agents available"),
    ("compaction-summary", "Earlier conversation summary ("),
    ("identity", "You are an expert AI coding agent"),
    ("subagent-base", "You are a sub-agent (agent_type:"),
    ("tools", "You have tools: "),
    ("guidelines", "Guidelines:"),
    ("spoken-briefing", "Spoken briefing (voice read-aloud):"),
    ("spoken-briefing", "## Spoken briefing"),
    ("ask-user", "Interview the user (ask_user tool):"),
    ("ask-user", "## Interview the user"),
    ("offline-note", "Note: the workspace's owning device is offline"),
]

SEPARATOR = "\n\n---\n\n"
BASE_JOIN = "\n\n\n\n"
COMPACT_SUMMARY_PREFIX = (
    "Earlier conversation summary (decisions and state to continue from;"
    " the full transcript is preserved separately):"
)
# Legacy summaries came from the old destructive compaction path, which
# deleted the summarized transcript rows -- the preservation claim would be
# false for them, so they keep the unqualified label.
COMPACT_SUMMARY_PREFIX_LEGACY = (
    "Earlier conversation summary (decisions and state to continue from):"
)


def compact_summary_message(prompt_state: dict) -> dict | None:
    """Build the injected compaction-summary message for a prompt state.

    Returns None when there is no summary to inject. The label is chosen
    by the summary's source: prompt-only compaction leaves every
    transcript row in place, legacy compaction does not.
    """
    summary = (prompt_state.get("summary") or "").strip()
    if not summary:
        return None
    source = prompt_state.get("source") or ""
    prefix = (
        COMPACT_SUMMARY_PREFIX if source == "prompt" else COMPACT_SUMMARY_PREFIX_LEGACY
    )
    return {"role": "system", "content": prefix + "\n" + summary}


def _name_section(text: str) -> str:
    for name, prefix in SECTION_OPENINGS:
        if text.startswith(prefix):
            return name
    return "unrecognized"


def _split_sections(system_text: str) -> list[dict]:
    """Ordered section list for one system message.

    Fragments arrive joined either by the production '---' separator
    or by bare newlines inside the base blob, so slice by scanning for
    known section-opening lines anywhere in the text. Text before the
    first known opening stays attached to the first opening (the
    identity/override head). A fragment that matches no opening keeps
    the previous section's name when short (continuation body).
    """
    if not system_text:
        return []
    chunks = system_text.split(SEPARATOR) if SEPARATOR in system_text else [system_text]
    parts: list[str] = []
    for chunk in chunks:
        cuts = [0]
        for _, prefix in SECTION_OPENINGS:
            idx = chunk.find(prefix)
            if idx > 0:
                cuts.append(idx)
        cuts = sorted(set(cuts))
        for a, b in zip(cuts, cuts[1:] + [len(chunk)]):
            parts.append(chunk[a:b])
    named: list[dict] = []
    for index, part in enumerate(parts):
        name = _name_section(part)
        if (
            index
            and name == "unrecognized"
            and len(part) < 80
            and named
        ):
            name = named[-1]["name"]
        named.append(_section_entry(name, part))
    return named


def _section_entry(name: str, text: str) -> dict:
    raw = text.encode("utf-8")
    return {
        "name": name,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "text": text,
    }


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Tool-schema matrices
# ---------------------------------------------------------------------------

def _schema_names_for(windows: bool, remote_target: bool, host_windows: bool) -> set:
    """Names get_schemas() returns on each platform/target, derived from the
    real import-time append (tools.py:1316-1334). On a posix box the
    Windows-only schemas (powershell + 11 computer + 4 sandbox) are absent
    from TOOLS_SCHEMA, so the win-* matrix removes them; everything else
    passes through get_schemas() itself (MCP manager.schemas() is empty with
    no servers configured; install_git is included iff git is missing --
    recorded as observed)."""
    from backend.agent.tools import TOOLS_SCHEMA

    present = {s["function"]["name"] for s in TOOLS_SCHEMA}
    expected = set(present)
    if windows or (remote_target and host_windows):
        # get_schemas adds POWERSHELL_SCHEMA for a windows target -- a local
        # Windows machine or a remote WINDOWS host (either client OS).
        expected.add("powershell")
    if remote_target:
        # tools.py strips local-only sandbox tools for remote.
        expected -= {
            "sandbox_test", "sandbox_run", "sandbox_status", "sandbox_stop",
        }
    elif not windows:
        # posix local: TOOLS_SCHEMA never carries the Windows-only schemas;
        # on a Windows dev box, subtract them to get the posix shape.
        expected -= {
            "powershell", "sandbox_test", "sandbox_run", "sandbox_status",
            "sandbox_stop",
        }
    return expected


def _schemas_for_platform(windows: bool, remote_target: bool, host_windows: bool,
                          plan: bool) -> list:
    """The real get_schemas() output for the platform shape, with plan-mode's
    exit_plan appended exactly as the loop does (loop.py:1381-1384)."""
    _ensure_backend()
    from backend.agent import tools as tools_mod
    from backend.agent import remote as remote_mod

    workspace = None
    if remote_target:
        # get_schemas(workspace=None) would hit MCP + gitenv on the local
        # path and needs a resolvable host for the remote path; pass the
        # real remote workspace string and register the fixture host first.
        workspace = REMOTE_WS if _registered_fixture_host() else "remote:offline-host:"
    names = _schema_names_for(windows, remote_target, host_windows)
    with _platform_os_name(windows):
        schemas = tools_mod.get_schemas(workspace=workspace)
    if plan:
        schemas = schemas + [tools_mod.EXIT_PLAN_SCHEMA]
    observed = [s["function"]["name"] for s in schemas]
    missing = [n for n in sorted(names - set(observed))]
    if missing:
        raise RuntimeError(
            f"get_schemas() did not return the expected platform shape: "
            f"missing {missing} (windows={windows}, remote={remote_target})"
        )
    return schemas


# ---------------------------------------------------------------------------
# Remote fixture (no production seam): a real RemoteSession whose env_line()
# only reads the handshake info dict -- it never connects. Precedent:
# backend/tests/test_remote_workspace_target.py, test_agent.py:1091.
# ---------------------------------------------------------------------------

FIXTURE_HOST_ID = "fixture-host"
REMOTE_WS = f"remote:{FIXTURE_HOST_ID}:C:/fixture/project"
OFFLINE_WS = "remote:offline-host:C:/fixture/project"
LOCAL_WS = str(Path(tempfile.gettempdir()) / "yaah-manifest-local-ws")
# Stable manifest token substituted for the host-specific LOCAL_WS path in
# rendered sub-agent prompt output, so committed fixtures (and their
# bytes/sha256 fields) are machine-independent (#182 return trip).
LOCAL_WS_TOKEN = "<LOCAL_WS>"
# The fixture conversation's id is a DB autoincrement counter — it
# drifts between renders in one process — so it is canonicalized out
# here, to the same end as LOCAL_WS: reproducible manifests.
CHAT_ID_TOKEN = "<CHAT_ID>"

_FIXTURE_INFO = {
    "host_id": FIXTURE_HOST_ID,
    "hostname": "fixture-host",
    "os": "Windows",
    "os_version": "11",
    "machine": "AMD64",
    "windows": True,
    "git_bash": True,
    "workspace_root": "C:/fixture",
    "protocol": "1",
    "instance_id": "fixture-instance",
}


def _fixture_host():
    from backend.agent.remote import RemoteSession

    return RemoteSession("http://fixture.invalid:9", "fixture-passphrase",
                         dict(_FIXTURE_INFO))


def _registered_fixture_host():
    from backend.agent import remote as remote_mod

    return remote_mod.get_remote(FIXTURE_HOST_ID) is not None


def _install_fixture_host() -> None:
    """Register the fixture host (make_active=True) -- idempotent."""
    from backend.agent import remote as remote_mod

    if not _registered_fixture_host():
        remote_mod.register_remote(_fixture_host(), make_active=True)


def _reset_remotes() -> None:
    from backend.agent import remote as remote_mod

    remote_mod.clear_remote()


# ---------------------------------------------------------------------------
# Render matrix: 192 local + 14 remote + 4 remote-offline + 6 kind combos.
# ---------------------------------------------------------------------------

def _local_flags() -> list:
    flags = ["plan", "skills", "memory"]
    flags += ["override", "compaction", "policy"]
    return flags


def _iter_local(windows: bool) -> list:
    """Every local combo id, generated from toggles (2^6 on each platform:
    combo ids enumerate plan/skills/memory/override/compaction/policy; the
    former screenshot (noshot) axis died with host computer use (#339)."""
    out = []
    prefix = "win" if windows else "posix"
    for plan in (0, 1):
        for skills in (0, 1):
            for memory in (0, 1):
                for override in (0, 1):
                    for compaction in (0, 1):
                        for policy in (0, 1):
                            parts = [f"{prefix}-local"]
                            if plan:
                                parts.append("plan")
                            if not skills:
                                parts.append("noskills")
                            if not memory:
                                parts.append("nomemory")
                            if override:
                                parts.append("override")
                            if compaction:
                                parts.append("compaction")
                            if policy:
                                parts.append("sandboxonly")
                            out.append("-".join(parts))
    return out


_REMOTE_VARIANTS = [
    "normal", "plan", "noskills", "nomemory", "compaction", "sandboxonly",
    "plan-skills-memory-compaction-sandboxonly",
]


def _iter_remote() -> list:
    out = []
    for prefix in ("win", "posix"):
        for variant in _REMOTE_VARIANTS:
            out.append(f"{prefix}-remote-{variant}")
        for variant in ("normal", "plan"):
            out.append(f"{prefix}-remote-offline-{variant}")
    return out


_KIND_COMBOS = [
    "kind-subagents-win-skills",
    "kind-subagents-win-noskills",
    "kind-subagents-posix-skills",
    "kind-subagents-posix-noskills",
    "kind-auxiliary-prompts",
]


def _combo_targets_windows(combo: str) -> bool:
    """Which platform a combo renders for (kind combos carry their
    platform in the id; the auxiliary kind is host-neutral)."""
    if combo.startswith("win-"):
        return True
    if combo.startswith("posix-"):
        return False
    if combo.startswith("kind-subagents-win"):
        return True
    if combo.startswith("kind-subagents-posix"):
        return False
    return True  # kind-auxiliary-prompts


def combos_for_host() -> list:
    """Combos renderable on THIS host (target platform must match)."""
    return [
        c
        for c in iter_combos()
        if _combo_targets_windows(c) == HOST_WINDOWS
    ]


def iter_combos() -> list:
    """All combo ids in the render matrix (deterministic order)."""
    return _iter_local(True) + _iter_local(False) + _iter_remote() + list(_KIND_COMBOS)


def _split_combo(combo: str) -> dict:
    parts = combo.split("-")
    if combo.startswith(("kind-",)):
        return {
            "kind": "subagents" if "subagents" in parts else "auxiliary",
            "plan": False,
            "skills": "noskills" not in parts,
            # #182: kind-subagents seeds a memory index so the manifest
            # shows the chosen invariant (memory section iff the resolved
            # schemas include a memory tool).
            "memory": "subagents" in parts,
                "override": False,
            "compaction": False,
            "policy": False,
        }
    flags = {
        "plan": "plan" in parts,
        "skills": "noskills" not in parts,
        "memory": "nomemory" not in parts,
        "override": "override" in parts,
        "compaction": "compaction" in parts,
        "policy": "sandboxonly" in parts,
    }
    family = parts[1]
    if family == "remote":
        variant = "-".join(parts[2:])
        kind = "remote-offline" if variant.startswith("offline") else "remote"
        flags = {
            "plan": "plan" in parts,
            "skills": "noskills" not in parts,
            "memory": "nomemory" not in parts,
            "override": False,
            "compaction": "compaction" in parts,
            "policy": "sandboxonly" in parts,
        }
        return {"kind": kind, **flags}
    return {"kind": family, **flags}


# ---------------------------------------------------------------------------
# Renderers -- each drives the REAL entry points only.
# ---------------------------------------------------------------------------

from contextlib import contextmanager


@contextmanager
def _platform_os_name(windows: bool):
    """Flip os.name for the duration when the target differs from the
    host. On a posix host rendering posix, this is a no-op (native).
    On a Windows host rendering posix, pathlib.Path dispatches on
    os.name at call time, so the flip also rebinds backend modules'
    Path names to a WindowsPath subclass (subclasses skip pathlib's
    os guard) and restores them on exit. The mirror case (posix host
    rendering windows) needs the same rebinding: os.name flips to
    'nt', so a plain pathlib.Path() call under the flip would try to
    instantiate a real WindowsPath and raise
    'cannot instantiate WindowsPath on your system' (#184 Linux CI).
    """
    import os as _os
    import sys as _sys

    target_windows = bool(windows)
    if target_windows == HOST_WINDOWS:
        yield
        return
    import pathlib as _pathlib

    # pathlib's host guard ("cannot instantiate WindowsPath on your
    # system") is DEFINED INSIDE the concrete WindowsPath class when
    # os.name != 'nt' at class-creation time -- so on a posix host, a
    # WindowsPath SUBCLASS inherits the raising __new__ and still raises
    # under the flip (#184 Linux CI, merge heads 3897049/1e18cd2). The
    # guard is asymmetric: on a Windows host WindowsPath carries no
    # guard, which is why this only ever failed on Linux. Shed the
    # inherited guard with a trivial __new__ that calls object.__new__
    # (WindowsPath.__init__/__slots__ do the rest); the class keeps the
    # full concrete API (mkdir/write_text/iterdir/resolve).
    class _AlwaysWinPath(_pathlib.WindowsPath):
        def __new__(cls, *args, **kwargs):
            return object.__new__(cls)

    swapped = []
    for mod_name, mod in list(_sys.modules.items()):
        if mod_name.startswith("backend") and getattr(mod, "Path", None) is _pathlib.Path:
            mod.Path = _AlwaysWinPath
            swapped.append((mod, _pathlib.Path))
    _os.name = "nt" if target_windows else "posix"
    try:
        yield
    finally:
        _os.name = "nt" if HOST_WINDOWS else "posix"
        for mod, orig in swapped:
            mod.Path = orig


def _isolated_roots() -> dict:
    """Throwaway DB/config/memory roots (already redirected by conftest under
    pytest; by _bootstrap_env under the CLI)."""
    _ensure_backend()
    from backend.agent.config import CONFIG_PATH
    from backend.db.database import DB_PATH

    return {"config": CONFIG_PATH, "db": DB_PATH}


def _prep_flags(flags: dict, tmp: Path) -> None:
    """Materialize one combination's environment in the redirected seams."""
    _ensure_backend()
    import backend.agent.loop, backend.agent.subagents  # noqa: F401
    from backend.agent import remote as remote_mod
    from backend.agent import skills as skill_registry
    from backend.agent import subagents as subagents_mod
    from backend.agent.config import save_config

    remote_mod.clear_remote()
    if flags["kind"] == "remote":
        _install_fixture_host()

    cfg = {
        "access_mode": "plan" if flags["plan"] else "full",
    }
    save_config(cfg)

    import shutil

    skills_dir = Path(os.environ["YAAH_SKILLS_PATH"])
    shutil.rmtree(skills_dir, ignore_errors=True)
    if flags["skills"]:
        for name in ("tdd", "code-review"):
            d = skills_dir / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: manifest fixture skill "
                f"{name} for the render matrix.\n---\n\n{noop_skill_body(name)}\n",
                encoding="utf-8",
            )
    skill_registry.scan_skills()

    from backend.agent import memory as memory_mod

    # Issue #169: the prompt gate now also consults memory.enabled, so the
    # fixture flips the config flag in lockstep with the index fixture.
    from backend.agent import config as config_mod

    config_mod.save_config({"memory": {"enabled": bool(flags["memory"])}})

    workspace = (
        REMOTE_WS if flags["kind"] == "remote" else LOCAL_WS
    )
    idx = memory_mod._index_path(workspace)
    if flags['memory']:
        # Write through the REAL index path: memory indexes are
        # project-keyed under memory_root()/<project_key(ws)>/MEMORY.md.
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text(
            "# Persistent memory"
            + chr(10) * 2
            + "- [fixture-memory](fixture-memory.md): render-matrix "
              "fixture memory entry."
            + chr(10),
            encoding="utf-8",
        )
    else:
        idx.unlink(missing_ok=True)
    if flags["kind"] == "local":
        (Path(LOCAL_WS) / "AGENTS.md").write_text(
            "# YAAH\n\nRender-matrix fixture workspace notes: the harness "
            "verifies the project-notes injection path, not this text.",
            encoding="utf-8",
        )
    subagents_mod.scan_agents()


def noop_skill_body(name: str) -> str:
    return (
        f"Fixture skill '{name}' for the prompt render matrix. Instructions "
        "are deliberately generic: the harness measures assembly, not skill "
        "content. Do nothing harmful; report done."
    )


def _drive_turn(flags: dict) -> dict:
    """One real pass through loop._run_agent_claimed.

    The model client is scripted so nothing leaves the machine; the chat()
    monkeypatch captures exactly the messages/tools the loop would send.
    _run_agent_claimed is fully DB-fakeable -- get_db() auto-creates the
    schema on the redirected throwaway DB (conftest precedent)."""
    import asyncio

    _ensure_backend()
    from backend.agent import loop
    from backend.db.database import (
        add_message, compact_conversation, create_conversation,
        delete_conversation, update_conversation,
    )

    captured: dict = {}

    async def fake_chat(messages, tools=None, stream=True, **kwargs):
        captured["messages"] = [dict(m) for m in messages]
        captured["tools"] = list(tools or [])
        captured["kwargs"] = dict(kwargs)

        async def _stream():
            yield {"type": "content", "text": "fixture turn complete."}
            yield {"type": "finish"}
        return _stream()

    async def _run() -> dict:
        # Restore the real chat after the scripted turn: a leaked fake_chat
        # silently re-scripted every later model call in the process (tests
        # running after a render test saw no model traffic at all).
        orig_chat = loop.model_client.chat
        loop.model_client.chat = fake_chat
        cid = None
        try:
            ws_dir = Path(LOCAL_WS)
            ws_dir.mkdir(parents=True, exist_ok=True)
            workspace = str(ws_dir)
            # the fixture workspace is a real local git repo — seed one
            # once (idempotent) so git reads in the render matrix work.
            if not (ws_dir / ".git").exists():
                import subprocess

                subprocess.run(
                    ["git", "init", "-q", "-b", "master", str(ws_dir)],
                    check=True, capture_output=True,
                )
                subprocess.run(
                    ["git", "-C", str(ws_dir),
                     "-c", "user.email=fixture@yaah.local",
                     "-c", "user.name=Fixture",
                     "commit", "-q", "--allow-empty", "-m", "fixture"],
                    check=True, capture_output=True,
                )
            if flags["kind"] == "remote":
                workspace = REMOTE_WS
            elif flags["kind"] == "remote-offline":
                workspace = OFFLINE_WS
            cid = await create_conversation("manifest", workspace=workspace)
            if flags["override"]:
                await update_conversation(
                    cid,
                    system_prompt_override="You are a pirate. Speak in pirate.",
                )
            await add_message(cid, "user", "fixture first user message")
            if flags["compaction"]:
                # Persist a summary through the REAL compaction persistence
                # (compact_conversation) -- exactly what _maybe_compact leaves
                # behind; run_agent_turn reads it via get_prompt_summary.
                # The watermark must be THIS conversation's message id, not a
                # literal: #184 -- a stale literal is rejected by
                # compact_conversation's monotonic-watermark guard and the
                # summary silently never persists.
                through_id = await add_message(
                    cid, "assistant", "fixture prior assistant reply"
                )
                await compact_conversation(
                    cid,
                    "Fixture summary of the earlier conversation.",
                    through_id,
                )
            policy = "sandbox-only" if flags["policy"] else None
            async for _event in loop._run_agent_claimed(
                cid, "fixture user message", workspace,
                policy=policy,
            ):
                pass
        finally:
            loop.model_client.chat = orig_chat
            if cid is not None:
                await delete_conversation(cid)
        return captured

    return asyncio.run(_run())


def render_local_family(combo: str, flags: dict) -> dict:
    """Full chat-turn manifest: run_agent_turn's system message(s), tools,
    and schema summaries. The offline note comes from the production
    remote_runner._system_prompt wrapper."""
    # The bootstrap warm-up turn must ALWAYS render host-native: it
    # exists to run lazy imports (pydantic plugin scan, DB migration)
    # while os.name is real, so it must never enter a flip.
    windows = (
        HOST_WINDOWS if combo == "warmup" else _combo_targets_windows(combo)
    )
    offline = flags["kind"] == "remote-offline"
    if not windows:
        import backend.agent.loop  # noqa: F401 (import under real os.name)
    with _platform_os_name(windows):
        captured = _drive_turn(flags)
        if offline:
            from backend.agent import remote as remote_mod
            from backend.agent.remote_runner import _system_prompt

            captured["messages"][0]["content"] = _system_prompt(
                OFFLINE_WS, remote_mod.get_remote("offline-host")
            )
    if not windows:
        allowed = _schema_names_for(
            False, flags["kind"] != "local", flags["kind"] != "local"
        )
        if flags["plan"]:
            # loop.py appends EXIT_PLAN_SCHEMA when plan mode is on (after
            # the captured turn); the platform-shape filter must not strip
            # it or plan-vs-normal combos collapse to identical tool sets
            # on posix renders (issue #178 CI failure).
            allowed = allowed | {"exit_plan"}
        captured["tools"] = [
            s
            for s in captured["tools"]
            if s.get("function", {}).get("name") in allowed
        ]
    messages = captured["messages"]
    system_messages = [m for m in messages if m.get("role") == "system"]
    sep = chr(10) * 2 + "====" + chr(10) * 2
    rendered = _canonicalize_chat_ids(
        sep.join(str(m.get("content") or "") for m in system_messages)
    )
    tools = captured["tools"]
    return {
        "combo": combo,
        "kind": flags["kind"],
        # Sections across ALL system messages: the compaction summary is
        # injected as its own system message (loop.py), so slicing only
        # the first one hid it from every manifest (#184).
        "sections": [
            section
            for m in system_messages
            for section in _split_sections(
                _canonicalize_chat_ids(str(m.get("content") or ""))
            )
        ],
        "system_messages": [
            {
                "role": "system",
                "bytes": len(_canonicalize_chat_ids(str(m.get("content") or "")).encode("utf-8")),
                "sha256": _sha256_text(_canonicalize_chat_ids(str(m.get("content") or ""))),
            }
            for m in system_messages
        ],
        "total_bytes": len(rendered.encode("utf-8")),
        "rendered_sha256": _sha256_text(rendered),
        "tool_schemas": _tool_schemas_summary(tools),
        "subagent_prompts": {},
        "auxiliary_prompts": {},
        "rendered_text": rendered,
    }


def _tool_schemas_summary(tools: list) -> list:
    out = []
    for schema in tools or []:
        fn = schema.get("function") or {}
        raw = json.dumps(schema, sort_keys=True).encode("utf-8")
        out.append({
            "name": fn.get("name") or "?",
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    return out


def _def_to_manifest(defn) -> dict:
    """A built-in AgentDef through _sub_agent_system_prompt (the REAL
    builder). Tool list is the definition's static prose list, not
    get_schemas -- matched by matching the production section names.
    Workspace is LOCAL_WS so the #182 memory-index injection (seeded by
    _prep_flags for kind-subagents) renders when the resolved schemas
    include a memory tool."""
    from backend.agent.subagents import _sub_agent_system_prompt

    prompt = _sub_agent_system_prompt(defn, LOCAL_WS)
    # Canonicalize the host-specific workspace path out of the rendered
    # output before sizing/hashing/splitting, so committed manifests are
    # reproducible on any machine (#182 return trip). Injections that
    # resolve the workspace (project notes) emit the long real path, so
    # both the LOCAL_WS spelling and its resolved form are replaced.
    for variant in {LOCAL_WS, str(Path(LOCAL_WS).resolve())}:
        prompt = prompt.replace(variant, LOCAL_WS_TOKEN)
    raw = prompt.encode("utf-8")
    return {
        "name": defn.name,
        "bytes": len(raw),
        "sha256": _sha256_text(prompt),
        "sections": _split_sections(prompt),
        "tools_allowlist": list(defn.tools) if defn.tools is not None else None,
        "text": prompt,
    }


def render_subagents(combo: str, flags: dict) -> dict:
    """kind-subagents-*: both built-in sub-agent system prompts, via
    subagents._sub_agent_system_prompt (bodies + env line + static prose
    tool list + guidelines + skills index). skills/noskills flips the
    fixture skill dir so the skills-index section appears/disappears."""
    from backend.agent.subagents import get_agent_def

    prompts = {}
    with _platform_os_name(_combo_targets_windows(combo)):
        for name in ("general-purpose", "explore"):
            defn = get_agent_def(name)
            if defn is None:
                raise RuntimeError(f"built-in sub-agent {name} missing")
            prompts[name] = _def_to_manifest(defn)
    rendered = list(prompts.values())[0]["text"]
    total = sum(entry["bytes"] for entry in prompts.values())
    return {
        "combo": combo,
        "kind": "subagents",
        "sections": [],
        "system_messages": [],
        "total_bytes": total,
        "rendered_sha256": _sha256_text(
            chr(10).join(e["sha256"] for e in prompts.values())
        ),
        "tool_schemas": [],
        "subagent_prompts": prompts,
        "auxiliary_prompts": {},
        "rendered_text": rendered,
    }


def render_auxiliary(combo: str, flags: dict) -> dict:
    """kind-auxiliary-prompts: auxiliary model-call prompts CAPTURED by
    driving their real producers with a scripted model client -- the
    compaction summarizer through compaction.summarize_messages, the title
    prompt through loop._generate_conversation_title. No prompt text is
    copied into this module."""
    import asyncio

    _ensure_backend()
    from backend.agent import compaction as compaction_mod
    from backend.agent import loop as loop_mod
    from backend.db.database import (
        add_message, create_conversation, delete_conversation,
        update_conversation,
    )

    captured_calls: list = []

    def fake_chat(messages, tools=None, stream=True, **kwargs):
        captured_calls.append([dict(m) for m in messages])
        if stream:
            async def _stream():
                yield {"type": "content", "text": "fixture auxiliary response"}
                yield {"type": "finish"}
            return _stream()
        async def _once():
            return "fixture auxiliary response"
        return _once()

    async def _capture() -> None:
        loop_mod.model_client.chat = fake_chat
        await compaction_mod.summarize_messages([
            {"role": "user", "content": "fixture transcript one"},
            {"role": "assistant", "content": "fixture transcript two"},
        ])
        cid = await create_conversation("manifest")
        await update_conversation(cid, title="fixture user message")
        await add_message(cid, "user", "fixture user message")
        await loop_mod._generate_conversation_title(cid, "fixture user message")
        await delete_conversation(cid)

    asyncio.run(_capture())
    summarizer_system = captured_calls[0][0]["content"]
    title_system = captured_calls[1][0]["content"]
    title_user = captured_calls[1][1]["content"]
    prompts = {
        "compaction_summarizer": {
            "builder": "backend/agent/compaction.py:summarize_messages (captured)",
            "text": summarizer_system,
        },
        "title_generation": {
            "builder": "backend/agent/loop.py:_generate_conversation_title (captured)",
            "text": title_system + chr(10) * 2 + "[user] " + title_user,
        },
    }
    out = {}
    total = 0
    for name, entry in prompts.items():
        raw = entry["text"].encode("utf-8")
        total += len(raw)
        out[name] = {
            "builder": entry["builder"],
            "bytes": len(raw),
            "sha256": _sha256_text(entry["text"]),
            "text": entry["text"],
        }
    return {
        "combo": combo,
        "kind": "auxiliary",
        "sections": [],
        "system_messages": [],
        "total_bytes": total,
        "rendered_sha256": _sha256_text(
            chr(10).join(e["sha256"] for e in out.values())
        ),
        "tool_schemas": [],
        "subagent_prompts": {},
        "auxiliary_prompts": out,
        "rendered_text": "",
    }


def render_combo(combo: str) -> dict:
    """Render one manifest for a combo id (the single public entry)."""
    flags = _split_combo(combo)
    kind = flags["kind"]
    if kind in ("subagents", "auxiliary"):
        _prep_flags(flags, None)
        if kind == "auxiliary":
            return render_auxiliary(combo, flags)
        return render_subagents(combo, flags)
    _prep_flags(flags, None)
    return render_local_family(combo, flags)


# ---------------------------------------------------------------------------
# Manifest serialization + batch generation
# ---------------------------------------------------------------------------

def _canonicalize_chat_ids(text: str) -> str:
    """Replace every `chat-<id>` spelling (the fixture conversation id is
    a DB autoincrement counter, not data) with the `<CHAT_ID>` token
    before sizing/hashing/splitting: reproducible manifests."""
    return re.sub(r"chat-\d+", CHAT_ID_TOKEN, text)


def manifest_to_json(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, sort_keys=False, ensure_ascii=False) + chr(10)


def write_manifest(manifest: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{manifest['combo']}.json"
    path.write_bytes(
        manifest_to_json(manifest).encode("utf-8")
    )
    return path


def generate_all(out_dir: str | Path = "backend/prompt_manifests") -> list:
    """Render every HOST-renderable combo (one manifest per combo).

    The other platform's manifests stay as committed reference
    artifacts; canonical bytes come from each platform's own host.
    """
    out_dir = Path(out_dir)
    written = []
    for combo in combos_for_host():
        manifest = render_combo(combo)
        written.append(write_manifest(manifest, out_dir))
    return written


# ---------------------------------------------------------------------------
# CLI -- a reviewer can dump any combination with one command.
# ---------------------------------------------------------------------------

def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.agent.prompt_manifest",
        description="Render YAAH's prompt render matrix into committed manifests.",
    )
    parser.add_argument("--list", action="store_true",
                        help="list every combo id in the render matrix")
    parser.add_argument("--render", metavar="COMBO",
                        help="render one combo; prints the manifest JSON "
                             "(or writes it with --out)")
    parser.add_argument("--all", action="store_true",
                        help="render the whole matrix into --out-dir")
    parser.add_argument("--out", metavar="FILE",
                        help="write --render output to FILE instead of stdout")
    parser.add_argument("--out-dir", metavar="DIR", default="backend/prompt_manifests",
                        help="target dir for --all (default: backend/prompt_manifests)")
    args = parser.parse_args(argv)

    if args.list:
        for combo in iter_combos():
            print(combo)
        return 0
    if args.render:
        manifest = render_combo(args.render)
        payload = manifest_to_json(manifest)
        if args.out:
            Path(args.out).write_text(payload, encoding="utf-8")
            print(f"wrote {args.out}")
        else:
            sys.stdout.write(payload)
        return 0
    if args.all:
        written = generate_all(args.out_dir)
        print(f"wrote {len(written)} manifests to {args.out_dir}")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
