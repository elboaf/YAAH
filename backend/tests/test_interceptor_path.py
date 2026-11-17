"""Tool-process PATH augmentation for Interceptor (ticket t4-path, #634).

Right after YAAH installs Interceptor, agent tool shells must be able to
run `interceptor` without restarting YAAH: the spawn path prepends the
install dir to the PATH of every process it launches, keyed off the
manager's install state -- and when Interceptor is absent the spawned
environment is byte-identical to the pre-feature one.

The tests isolate every gate input (done-marker, install-dir candidates)
into tmp dirs so neither the host's real Interceptor state nor a prior
test's can leak in. One test spawns a real probe process per AC5: the
child resolves a fake `interceptor`-style command that exists ONLY
because the augmented PATH carried the install dir -- the strongest
proof the spawned environment is augmented (and its negative twin proves
the resolution came from the augmentation, not the base PATH).
"""
import os
import subprocess
from pathlib import Path

import pytest

from backend.agent import ghenv, interceptor, tools
from backend.agent import interceptor_install as inst

IS_WINDOWS = os.name == "nt"
PROBE_NAME = "yaah-t4-path-probe"


# --- fixtures / seeders ---------------------------------------------------


@pytest.fixture
def state_root(monkeypatch, tmp_path):
    """Isolate every gate input the augmentation keys off.

    Redirects the install flow's done-marker to a tmp path (absent unless
    a test writes it) and the manager's install-dir candidates to a tmp
    dir (empty unless a test puts a CLI in it).
    """
    installdir = tmp_path / "Interceptor"
    monkeypatch.setattr(interceptor, "_INSTALL_DIR_CANDIDATES", (str(installdir),))
    marker_dir = tmp_path / "yaah-state"
    marker_dir.mkdir()
    monkeypatch.setattr(inst, "_INSTALL_TMP", marker_dir)
    monkeypatch.setattr(
        inst, "_DONE_MARKER_PATH", marker_dir / "interceptor_install_done.txt"
    )
    return installdir


def _write_marker() -> None:
    """Simulate t2's post-install marker write."""
    inst._DONE_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    inst._DONE_MARKER_PATH.write_text("done", encoding="utf-8")


def _install_cli(installdir: Path) -> None:
    """Lay down the CLI the installer would have dropped into the dir."""
    installdir.mkdir(parents=True, exist_ok=True)
    # The gate probes interceptor.exe unconditionally (mirroring
    # _find_interceptor_cli), on every platform.
    (installdir / "interceptor.exe").write_bytes(b"fake")


def _write_probe(installdir: Path) -> None:
    """A fake CLI-shaped command that exists nowhere else on the machine.

    It prints PROBE_OK when a shell resolves and runs it; the Windows form
    also echoes its own directory (%~dp0) so the test can see WHERE it
    resolved from.
    """
    installdir.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        probe = installdir / f"{PROBE_NAME}.cmd"
        probe.write_text("@echo PROBE_OK %~dp0\r\n", encoding="utf-8")
    else:
        probe = installdir / PROBE_NAME
        probe.write_text("#!/bin/sh\necho PROBE_OK\n", encoding="utf-8")
        probe.chmod(0o755)


def _first_path_entry(env: dict) -> str:
    """The front of the env's PATH, whatever casing the key arrived in."""
    path_key = ghenv._find_path_key(env)
    separator = ";" if os.name == "nt" else os.pathsep
    return env[path_key].split(separator)[0]


def _path_entries(env: dict) -> list[str]:
    path_key = ghenv._find_path_key(env)
    separator = ";" if os.name == "nt" else os.pathsep
    return env[path_key].split(separator)


# --- AC1: spawned tool processes see the install dir on PATH --------------


def test_tool_env_prepends_install_dir_after_install(state_root):
    """A tool process spawned after install gets the install dir at the
    front of its PATH -- a probe child can resolve the CLI immediately."""
    _write_marker()
    _install_cli(state_root)

    env = tools._tool_env()
    assert _first_path_entry(env) == str(state_root)
    # And the dir genuinely holds the CLI the child would resolve.
    assert (Path(_first_path_entry(env)) / "interceptor.exe").is_file()


def test_ghenv_seam_carries_manager_dir(monkeypatch, state_root):
    """The one seam ghenv exposes mirrors the manager's decision, both
    directions (present -> dir, absent -> None)."""
    _write_marker()
    _install_cli(state_root)
    assert ghenv.interceptor_prepend_dir() == str(state_root)

    monkeypatch.setattr(interceptor, "path_prepend_dir", lambda: None)
    assert ghenv.interceptor_prepend_dir() is None


# --- AC2: no backend restart required -------------------------------------


def test_install_mid_session_is_seen_by_next_spawn(state_root):
    """The gate re-reads its inputs on every spawn: a marker written after
    the backend started (the in-app install's exact flow) is picked up by
    the next tool process, with no restart and no cached state."""
    before = tools._tool_env()
    assert str(state_root) not in _path_entries(before)

    _write_marker()  # install() writes this at the end -- nothing restarts
    _install_cli(state_root)

    after = tools._tool_env()
    assert _first_path_entry(after) == str(state_root)


# --- AC3: byte-identical PATH when Interceptor is absent -------------------


def test_absent_interceptor_leaves_env_byte_identical(state_root):
    """No marker (never installed): _tool_env must equal plain command_env
    -- the whole dict, not just PATH."""
    baseline = ghenv.command_env()
    env = tools._tool_env()
    assert env == baseline


def test_partial_install_without_marker_not_advertised(state_root):
    """A CLI on disk without a full-install marker is a partial install:
    the gate stays closed and PATH is untouched."""
    _install_cli(state_root)  # no marker
    assert interceptor.path_prepend_dir() is None
    assert tools._tool_env() == ghenv.command_env()


def test_marker_with_missing_cli_not_advertised(state_root):
    """A marker whose install dir has since been wiped (external
    uninstall): nothing is advertised -- a PATH entry pointing at nothing
    must not be prepended."""
    _write_marker()  # no CLI in the dir
    assert interceptor.path_prepend_dir() is None
    assert tools._tool_env() == ghenv.command_env()


def test_gate_fails_closed_when_marker_read_raises(monkeypatch, state_root):
    """A marker-read error reads as 'not done': the augmentation gates a
    PATH convenience, never a crash."""
    _write_marker()
    _install_cli(state_root)

    def boom():
        raise RuntimeError("marker read exploded")

    monkeypatch.setattr(inst, "_marker_done", boom)
    assert interceptor.path_prepend_dir() is None


# --- AC4: no unbounded PATH growth across repeated spawns ------------------


def test_repeated_spawns_do_not_grow_path(monkeypatch, state_root):
    """Every spawn builds its env from the parent's PATH and dedupe-prepends
    the dir once: repeated spawns are stable, and an already-augmented PATH
    (a child YAAH inheriting one) does not grow it."""
    _write_marker()
    _install_cli(state_root)

    envs = [tools._tool_env() for _ in range(5)]
    paths = {env[ghenv._find_path_key(env)] for env in envs}
    assert len(paths) == 1, "repeated spawns changed PATH"

    occurrences = sum(
        1
        for entry in _path_entries(envs[0])
        if entry.casefold() == str(state_root).casefold()
    )
    assert occurrences == 1, "install dir prepended more than once"

    # A second YAAH process inheriting the augmented PATH (its os.environ
    # already carries the dir) must not append a second copy.
    monkeypatch.setattr(ghenv, "interceptor_prepend_dir", lambda: str(state_root))
    inherited = dict(envs[0])
    sim = ghenv.command_env(inherited)
    ghenv._prepend_to_path(sim, str(state_root))
    assert sum(
        1
        for entry in _path_entries(sim)
        if entry.casefold() == str(state_root).casefold()
    ) == 1


# --- AC5: a real probe process asserts the augmented environment -----------


async def _probe(workspace: Path) -> dict:
    """Run the probe command the way the agent's tool shells do."""
    if IS_WINDOWS:
        # PowerShell resolves bare command names through PATH with PATHEXT,
        # so the bare probe name finds the fake .cmd.
        return await tools.run_powershell(str(workspace), PROBE_NAME)
    return await tools.run_bash(str(workspace), PROBE_NAME)


@pytest.mark.asyncio
async def test_probe_child_resolves_cli_immediately(state_root, tmp_path):
    """AC1/AC5 end-to-end: a real child process spawned through the tool
    path resolves the fake CLI immediately -- it exists only because the
    spawned PATH carried the install dir."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_marker()
    _install_cli(state_root)
    _write_probe(state_root)

    r = await _probe(workspace)
    assert r["exit_code"] == 0, r["output"]
    assert "PROBE_OK" in r["output"], r["output"]
    if IS_WINDOWS:
        # %~dp0: the child proves WHERE it resolved from.
        assert str(state_root).casefold() in r["output"].casefold()


@pytest.mark.asyncio
async def test_probe_child_does_not_resolve_without_augmentation(
    state_root, tmp_path
):
    """The negative twin: the same fake CLI on disk, but no done-marker --
    the child must fail to find it, proving the positive probe's
    resolution came from the augmented PATH and not the base one."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _install_cli(state_root)  # CLI present, gate closed (no marker)
    _write_probe(state_root)

    r = await _probe(workspace)
    assert "PROBE_OK" not in r["output"]


# --- the subprocess boundary is never used by the augmentation -------------


def test_augmentation_does_not_spawn_processes(monkeypatch, state_root):
    """The whole gate is filesystem reads; the PATH walk in _tool_env must
    never shell out (cheap enough for every single tool spawn)."""
    _write_marker()
    _install_cli(state_root)

    def no_spawn(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("PATH augmentation spawned a subprocess")

    monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    assert _first_path_entry(tools._tool_env()) == str(state_root)
