"""Issue #770: spawn_detached / stop_process (Stage 1).

External behavior only — did the child outlive the call, does the PID
answer, did the readiness probe report the truth, did stop kill the tree,
does the registry refuse a foreign PID — never the internal creation flags.
Windows-only: meaningless on POSIX, where there is no Job Object, so skip
there (the same skip style as the platform-conditional schema tests).
"""
import asyncio
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.agent.tools import execute_tool, get_schemas


# A child that appends one byte every `period` seconds for `n` iterations.
# Used as the heartbeat that proves a child keeps running after the tool
# call that started it returned.
HEARTBEAT_PY = (
    "import sys, time\n"
    "p, period, n = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])\n"
    "with open(p, 'a') as fh:\n"
    "    for _ in range(n):\n"
    "        fh.write('1'); fh.flush(); time.sleep(period)\n"
)


# A console program that writes a heartbeat file, so "did it actually run"
# is observable without reading its stdout (a console program's output to a
# file is block-buffered, so an empty log is not evidence of anything).
CONSOLE_BEAT_PY = (
    "import sys, time\n"
    "hb = sys.argv[1]\n"
    "with open(hb, 'a') as fh:\n"
    "    for _ in range(300):\n"
    "        fh.write('1'); fh.flush(); time.sleep(0.1)\n"
)


def _console_program():
    """A console-subsystem program -- the class DETACHED_PROCESS kills.
    powershell.exe ships with Windows; cmd.exe is the fallback."""
    return shutil.which("powershell") or shutil.which("cmd")


def _heartbeat_path(tmp_path) -> Path:
    hb = tmp_path / "heartbeat.txt"
    script = tmp_path / "_heartbeat.py"
    script.write_text(HEARTBEAT_PY)
    return hb


def _alive(pid) -> bool:
    try:
        import psutil

        return psutil.Process(pid).is_running()
    except Exception:
        return False


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


async def _growth(hb: Path, gap: float = 0.6) -> bool:
    """True if the heartbeat kept writing across `gap` seconds."""
    await asyncio.sleep(gap)
    s1 = _size(hb)
    await asyncio.sleep(gap)
    s2 = _size(hb)
    return s2 > s1


async def _wait_for_size(path: Path, *, min_bytes: int, timeout: float) -> bool:
    """Wait until `path` holds at least `min_bytes`, or `timeout` expires.

    Used to separate "the launcher started" from "the launcher initialized":
    `spawn_detached` returning started=True means the process was created,
    not that a shell launcher has finished starting its own child. Polling
    for the first output under a bounded deadline keeps a slow start from
    reading as a spawn-flags failure, while a launcher that exits without
    ever writing still fails when the deadline expires.
    """
    deadline = time.monotonic() + timeout
    while True:
        if _size(path) >= min_bytes:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)


def _cleanup():
    import backend.agent.detached as detached

    for pid in list(detached._REGISTRY):
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                           capture_output=True, timeout=10)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _no_leaks():
    """Ensure no test leaves a detached process running."""
    yield
    _cleanup()


@pytest.mark.skipif(os.name != "nt", reason="detached spawn is Windows-only")
@pytest.mark.skipif(platform.system() != "Windows",
                    reason="spawn_detached replaces a Windows Job Object")
@pytest.mark.asyncio
async def test_spawn_detached_survives_tool_call(tmp_path):
    """The load-bearing test: a child that writes a heartbeat keeps writing
    AFTER the tool call returns. It must be able to fail — the old run_bash
    path (Job Object) would show the heartbeat stopping."""
    hb = _heartbeat_path(tmp_path)
    argv = [sys.executable, str(tmp_path / "_heartbeat.py"), str(hb), "0.1", "60"]
    cmd = " ".join(argv)

    r = await execute_tool(
        "spawn_detached", {"command": cmd}, str(tmp_path)
    )
    assert r.get("started") is True, r
    assert r.get("pid") and isinstance(r["pid"], int)
    assert r.get("log_path")

    assert await _growth(hb), "heartbeat stopped after the call returned"


@pytest.mark.skipif(os.name != "nt", reason="detached spawn is Windows-only")
@pytest.mark.skipif(platform.system() != "Windows",
                    reason="spawn_detached replaces a Windows Job Object")
@pytest.mark.asyncio
async def test_bash_backgrounded_child_does_not_survive(tmp_path):
    """Negative control: the same heartbeat launched through the shell tool
    (which still owns the Job Object) dies the moment the call returns, so
    the heartbeat does NOT keep writing. Contrasts with the test above."""
    hb = _heartbeat_path(tmp_path)
    script = tmp_path / "_heartbeat.py"
    script.write_text(HEARTBEAT_PY)
    # POSIX path form: Git Bash mangles backslash drive paths into a Windows
    # drive spec, which python then cannot open.
    hb_posix = hb.as_posix()
    script_posix = script.as_posix()
    argv = [sys.executable, script_posix, hb_posix, "0.1", "60"]
    await execute_tool(
        "bash", {"command": " ".join(argv) + " &", "timeout_seconds": 0.3}, str(tmp_path)
    )
    assert await _growth(hb) is False, "backgrounded child outlived the shell call"


@pytest.mark.skipif(os.name != "nt", reason="detached spawn is Windows-only")
@pytest.mark.skipif(platform.system() != "Windows",
                    reason="spawn_detached replaces a Windows Job Object")
@pytest.mark.asyncio
async def test_outlives_call_and_persists_across_calls(tmp_path):
    """The child outlives both the call and (by staying in the registry) a
    later read: a fresh list_processes() still reports it alive, and it keeps
    writing. This is the property that protects a user's dev server across a
    backend restart."""
    hb = _heartbeat_path(tmp_path)
    argv = [sys.executable, str(tmp_path / "_heartbeat.py"), str(hb), "0.1", "60"]
    r = await execute_tool(
        "spawn_detached", {"command": " ".join(argv)}, str(tmp_path)
    )
    pid = r["pid"]
    assert _alive(pid)

    import backend.agent.detached as detached

    rows = {row["pid"]: row for row in detached.list_processes()}
    assert pid in rows and rows[pid]["alive"] is True
    assert await _growth(hb)


@pytest.mark.skipif(os.name != "nt", reason="detached spawn is Windows-only")
@pytest.mark.skipif(platform.system() != "Windows",
                    reason="spawn_detached replaces a Windows Job Object")
@pytest.mark.asyncio
async def test_stop_process_kills_tree(tmp_path):
    """Stop the parent and assert the parent AND its child are gone (the
    tree, not just the root)."""
    parent = tmp_path / "_parent.py"
    child = tmp_path / "_child.py"
    hbchild = tmp_path / "_childhb.txt"
    child.write_text(
        "import sys, time\n"
        "p, n = sys.argv[1], int(sys.argv[2])\n"
        "with open(p, 'a') as fh:\n"
        "    for _ in range(n):\n"
        "        fh.write('c'); fh.flush(); time.sleep(0.1)\n"
    )    # Derive paths from __file__ so the parent's source never interpolates a
    # Windows path (which would make Python treat C:\U... as an escape).
    parent.write_text(
        "import subprocess, sys, time\n"
        "here = __file__\n"
        "base = here[:-len('_parent.py')]\n"
        "c = subprocess.Popen([sys.executable, "
        "base + '_child.py', "
        "base + '_childhb.txt', '80'])\n"
        "open(base + 'childpid.txt', 'w').write(str(c.pid))\n"
        "time.sleep(8)\n"
    )
    r = await execute_tool(
        "spawn_detached",
        {"command": f"{sys.executable} _parent.py", "cwd": str(tmp_path)},
        str(tmp_path),
    )
    assert r.get("started") is True, r
    pid = r["pid"]
    # Let the parent spawn its child.
    for _ in range(20):
        tp = tmp_path / "childpid.txt"
        if tp.exists():
            break
        await asyncio.sleep(0.1)
    child_pid = int((tmp_path / "childpid.txt").read_text().strip())
    assert _alive(child_pid)

    stop = await execute_tool("stop_process", {"pid": pid}, str(tmp_path))
    assert stop.get("stopped") is True, stop

    # Give the tree a moment to (not) reappear.
    for _ in range(20):
        if not _alive(pid) and not _alive(child_pid):
            break
        await asyncio.sleep(0.1)
    assert not _alive(pid), "root still alive after stop"
    assert not _alive(child_pid), "child (tree) still alive after stop"


@pytest.mark.skipif(os.name != "nt", reason="detached spawn is Windows-only")
@pytest.mark.asyncio
async def test_stop_process_refuses_unknown_pid():
    """Safety: stopping a PID the registry never recorded is refused."""
    stop = await execute_tool("stop_process", {"pid": 999999}, "")
    assert stop.get("stopped") is not True
    assert "error" in stop


@pytest.mark.skipif(os.name != "nt", reason="detached spawn is Windows-only")
@pytest.mark.asyncio
async def test_registry_reports_dead_row_as_dead():
    """A registry row whose process has since exited reports dead, is pruned,
    and never reads as alive (registry honesty)."""
    import backend.agent.detached as detached

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    proc.wait(timeout=5)
    detached._record(
        pid, command="echo dead", cwd=str(Path(".")),
        log_path="/tmp/detached-test.log", conversation_id=None,
    )
    rows = detached.list_processes()
    assert all(row["pid"] != pid for row in rows), "dead row still listed"


@pytest.mark.skipif(os.name != "nt", reason="detached spawn is Windows-only")
@pytest.mark.asyncio
async def test_readiness_ready_then_not_ready(tmp_path):
    """Readiness reports ready once the port accepts, and not-ready (with log
    tail) when nothing listens — the probe never hangs past the timeout.

    The ready child holds a listening socket open for a few seconds; the
    probe should see the port accept. Then a not-ready probe against a port
    with nothing listening reports not-ready (with a log tail), never hangs.
    """
    import socket

    # Pick a free port the ready-child will bind and hold.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    # A child that binds `port`, accepts one connection, then exits.
    ready_child = tmp_path / "_ready_child.py"
    ready_child.write_text(
        "import socket, sys, time\n"
        "srv = socket.socket()\n"
        "srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "srv.bind(('127.0.0.1', int(sys.argv[1])))\n"
        "srv.listen(1)\n"
        "try:\n"
        "    conn, _ = srv.accept()\n"
        "    conn.close()\n"
        "except OSError:\n"
        "    pass\n"
        "time.sleep(0.5)\n"
        "srv.close()\n"
    )
    ready_cmd = [sys.executable, str(ready_child), str(port)]
    r = await execute_tool(
        "spawn_detached",
        {"command": " ".join(ready_cmd), "ready_port": port, "timeout_seconds": 5},
        "",
    )
    assert r.get("ready") is True, r

    # Nothing listens on port 1 → not-ready with a log tail, not a hang.
    # A short-lived real command so spawn *starts* but holds no port.
    r2 = await execute_tool(
        "spawn_detached",
        {"command": " ".join([sys.executable, "-c", "pass"]),
         "ready_port": 1, "timeout_seconds": 1},
        "",
    )
    assert r2.get("ready") is False, r2
    assert "log_tail" in r2 or "error" in r2


@pytest.mark.skipif(os.name != "nt", reason="spawn_detached schema is Windows-only")
def test_schemas_and_docs_include_spawn_detached():
    """The schema reaches the model and get_help agrees (doc/schema parity)."""
    names = {s["function"]["name"] for s in get_schemas()}
    assert "spawn_detached" in names
    assert "stop_process" in names
    from backend.agent import tools

    res = asyncio.run(tools.execute_tool("get_help", {}, workspace=""))
    help_names = {line.split(":")[0][2:] for line in res["tools"].splitlines()}
    assert "spawn_detached" in help_names


@pytest.mark.skipif(os.name != "nt", reason="spawn_detached schema is Windows-only")
@pytest.mark.asyncio
async def test_shell_prompt_fix_points_at_spawn_detached():
    """The false 'Start-Process/nohup' replacement is gone from the shell
    schemas; the corrected mechanism names spawn_detached."""
    names = {s["function"]["name"] for s in get_schemas()}
    if "powershell" in names:
        desc = next(
            s["function"]["description"]
            for s in get_schemas()
            if s["function"]["name"] == "powershell"
        )
        assert "spawn_detached" in desc
        assert "killed at timeout" not in desc


@pytest.mark.skipif(os.name != "nt", reason="spawn_detached schema is Windows-only")
@pytest.mark.asyncio
async def test_spawn_detached_refuses_duplicate_running(tmp_path):
    """A spawn whose command line is already running is refused (story 8)."""
    import backend.agent.detached as detached

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    detached._record(
        proc.pid, command="echo dup", cwd=str(tmp_path),
        log_path="/tmp/detached-dup.log", conversation_id=None,
    )
    try:
        r = await execute_tool("spawn_detached", {"command": "echo dup"}, str(tmp_path))
        assert r.get("started") is not True
        assert "already running" in (r.get("error") or "")
    finally:
        proc.kill()


@pytest.mark.skipif(os.name != "nt", reason="spawn_detached is Windows-only")
def test_resolved_launcher_with_spaces_is_quoted(tmp_path, monkeypatch):
    """A launcher resolved to a path with a space must be quoted as its own
    executable token, or CreateProcess splits the line at the space and picks
    the first existing prefix (a different program) instead.

    This drives the real spawn path end to end: the resolved launcher is a
    copy of the interpreter sitting in a spaced directory, with a decoy at the
    whitespace-truncation point. Pre-fix, the child that runs is the decoy (or
    the launch fails); post-fix it is the intended launcher.
    """
    spaced = tmp_path / "sp ace"
    spaced.mkdir()
    launcher = spaced / "launcher.exe"
    shutil.copy(sys.executable, launcher)
    decoy = tmp_path / "sp.exe"
    shutil.copy(sys.executable, decoy)

    script = tmp_path / "who.py"
    out = tmp_path / "ran.txt"
    script.write_text(
        "import sys\n"
        "open(sys.argv[1], 'w').write(sys.executable)\n"
    )
    monkeypatch.setenv("PATH", f"{spaced}{os.pathsep}{os.environ.get('PATH', '')}")

    r = asyncio.run(execute_tool(
        "spawn_detached",
        {"command": f"launcher.exe {script} {out}", "cwd": str(tmp_path)},
        str(tmp_path),
    ))
    assert r.get("started") is True, r

    deadline = time.time() + 20
    while time.time() < deadline and not out.exists():
        time.sleep(0.1)
    assert out.exists(), f"child never ran; result={r}"
    ran = out.read_text().strip()
    assert os.path.normcase(ran) == os.path.normcase(str(launcher)), (
        f"wrong executable ran: {ran!r} (intended {launcher}, decoy {decoy})"
    )


@pytest.mark.skipif(os.name != "nt", reason="spawn_detached is Windows-only")
def test_split_launcher_handles_quoted_and_bare_launchers():
    """The launcher token is split out of a Windows command line without
    losing the argument suffix, quoted or bare."""
    import backend.agent.detached as detached

    assert detached._split_launcher('npm run build') == ('npm', ' run build')
    assert detached._split_launcher('npm') == ('npm', '')
    assert detached._split_launcher(r'"C:\Program Files\node\npm.cmd" run build') == (
        r'C:\Program Files\node\npm.cmd', ' run build',
    )
    # Leading whitespace is preserved byte-for-byte (it rides in front of the
    # argument suffix, which is reattached unchanged after the executable).
    assert detached._split_launcher(r'  npm   run build') == ('npm', '     run build')


@pytest.mark.skipif(os.name != "nt", reason="spawn_detached is Windows-only")
def test_duplicate_refusal_normalizes_equivalent_cwd(tmp_path):
    """C:\\work\\app and C:\\work\\app\\. are one directory: a second spawn of
    the same command from an equivalent spelling is refused as a duplicate."""
    import backend.agent.detached as detached

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        detached._record(
            proc.pid, command="echo dup2", cwd=str(tmp_path),
            log_path="/tmp/detached-dup2.log", conversation_id=None,
        )
        # Same directory, spelled with a literal trailing "\." component
        # (pathlib would fold it away, so build the string directly).
        equivalent = str(tmp_path) + os.sep + "."
        assert equivalent != str(tmp_path), "test setup did not spell a variant"
        found = detached._find_duplicate("echo dup2", equivalent)
        assert found == proc.pid, (
            f"equivalent cwd not matched: {equivalent!r} vs {str(tmp_path)!r}"
        )
        # A different directory must still be allowed (not a duplicate).
        assert detached._find_duplicate("echo dup2", str(tmp_path / "other")) is None
    finally:
        proc.kill()


@pytest.mark.skipif(os.name != "nt", reason="detached spawn is Windows-only")
@pytest.mark.skipif(platform.system() != "Windows",
                    reason="spawn_detached replaces a Windows Job Object")
@pytest.mark.asyncio
async def test_spawn_detached_runs_a_console_program(tmp_path):
    """A CONSOLE program must run and keep running (regression, #770).

    The first round of these tests spawned only ``sys.executable``. Python is
    not a console-subsystem program, so it survived the DETACHED_PROCESS flag
    that killed powershell.exe and every .cmd shim (npm, npx, vite) -- exactly
    the dev-server case this tool exists for. The suite stayed green while the
    feature was broken for its main use case.

    This test spawns a console program, so it FAILS against the old flags:
    the child exits at once with code 0 and never writes a heartbeat. The
    heartbeat file is the assertion, never the child's log -- a console
    program's stdout redirected to a file is block-buffered, and that empty
    log is what made the original failure read as "the spawn worked".
    """
    exe = _console_program()
    if not exe:
        pytest.skip("no console program available on PATH")

    hb = tmp_path / "console_hb.txt"
    script = tmp_path / "_console_beat.py"
    script.write_text(CONSOLE_BEAT_PY)

    # PowerShell's -File accepts only .ps1, so launch python with -Command.
    # Paths are single-quoted for PowerShell, and an embedded apostrophe is
    # doubled (PowerShell's own escape inside a single-quoted string), so a
    # tmp_path like C:\Users\O'Brien\... still forms a valid argument.
    def ps_quote(value) -> str:
        return chr(39) + str(value).replace(chr(39), chr(39) * 2) + chr(39)

    if os.path.basename(exe).lower().startswith("powershell"):
        cmd = (
            f'"{exe}" -NoProfile -Command "& {ps_quote(sys.executable)} '
            f'{ps_quote(script)} {ps_quote(hb)}"'
        )
    else:
        cmd = f'"{exe}" /c ""{sys.executable}" "{script}" "{hb}""'

    r = await execute_tool("spawn_detached", {"command": cmd}, str(tmp_path))
    assert r.get("started") is True, r
    pid = r["pid"]
    assert isinstance(pid, int) and pid > 0

    try:
        # Wait for the FIRST heartbeat under a bounded deadline before
        # measuring growth: started=True confirms process creation, not that
        # the launcher has finished initializing. Measuring growth straight
        # away would read zero twice on a cold or loaded machine and report a
        # slow start as a spawn-flags failure. A launcher that exits without
        # ever writing still fails, when the deadline expires.
        wrote = await _wait_for_size(hb, min_bytes=1, timeout=15.0)
        assert wrote, (
            "console program wrote no heartbeat: it never ran. The spawn flags "
            "left it without a console (PowerShell/PowerShell#28383), and the "
            "tool still reported started=true"
        )
        # Then confirm it KEEPS running, not just that it started.
        assert await _growth(hb), "console program stopped after a first heartbeat"
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True)


# ---------------------------------------------------------------- list tool
# #770 story 7: the agent-facing listing. The panel route reads the same
# registry, but the agent had no way to ask "what did I start?" - after a
# backend restart, or from another chat, a running process was invisible.

@pytest.mark.skipif(os.name != "nt", reason="list_processes schema is Windows-only")
def test_list_processes_schema_and_help():
    """The schema reaches the model and get_help agrees (doc/schema parity)."""
    names = {s["function"]["name"] for s in get_schemas()}
    assert "list_processes" in names
    from backend.agent import tools

    res = asyncio.run(tools.execute_tool("get_help", {}, workspace=""))
    help_names = {line.split(":")[0][2:] for line in res["tools"].splitlines()}
    assert "list_processes" in help_names
    # No arguments: the registry is machine-wide, so a listing cannot be
    # scoped by the caller.
    schema = next(s for s in get_schemas()
                  if s["function"]["name"] == "list_processes")
    assert schema["function"]["parameters"]["properties"] == {}


def test_list_processes_is_read_classified():
    """Listing is observation, so it is free in every access mode."""
    from backend.agent.tools import tool_risk

    assert tool_risk("list_processes") == "read"


@pytest.mark.skipif(os.name != "nt", reason="detached spawn is Windows-only")
@pytest.mark.asyncio
async def test_executor_is_registered_and_remote_capable():
    """The name must resolve to an executor, and be advertised to a remote
    chat so a remote host lists ITS OWN registry (issue #770 stage 1 rule:
    the spawn happens on the host, so the list must read the host)."""
    from backend.agent import remote as remote_mod
    from backend.agent import tools as tools_mod

    assert "list_processes" in tools_mod.EXECUTORS
    assert "list_processes" in remote_mod.REMOTE_TOOLS


@pytest.mark.skipif(os.name != "nt", reason="detached spawn is Windows-only")
@pytest.mark.skipif(platform.system() != "Windows",
                    reason="spawn_detached replaces a Windows Job Object")
@pytest.mark.asyncio
async def test_list_tool_reports_a_live_spawned_process(tmp_path):
    """The real producer path: spawn through the tool, then LIST through the
    tool and find that exact process - with its command, not just a count.
    A swallowed result or an echoed-empty list fails this."""
    hb = _heartbeat_path(tmp_path)
    argv = [sys.executable, str(tmp_path / "_heartbeat.py"), str(hb), "0.1", "60"]
    cmd = " ".join(argv)

    r = await execute_tool("spawn_detached", {"command": cmd}, str(tmp_path))
    pid = r["pid"]

    listing = await execute_tool("list_processes", {}, str(tmp_path))
    rows = {row["pid"]: row for row in listing["processes"]}
    assert pid in rows, f"spawned pid {pid} missing from {listing}"
    assert rows[pid]["alive"] is True
    assert "_heartbeat.py" in rows[pid]["command"]
    assert listing["known"] == len(listing["processes"])


@pytest.mark.skipif(os.name != "nt", reason="detached spawn is Windows-only")
@pytest.mark.skipif(platform.system() != "Windows",
                    reason="spawn_detached replaces a Windows Job Object")
@pytest.mark.asyncio
async def test_list_tool_drops_a_stopped_process(tmp_path):
    """After stop_process, the listing no longer shows the row: the list is
    live state, not a replay of everything ever spawned."""
    cmd = " ".join([sys.executable, "-c", "import time; time.sleep(60)"])
    r = await execute_tool("spawn_detached", {"command": cmd}, str(tmp_path))
    pid = r["pid"]

    before = await execute_tool("list_processes", {}, str(tmp_path))
    assert pid in {row["pid"] for row in before["processes"]}

    stopped = await execute_tool("stop_process", {"pid": pid}, str(tmp_path))
    assert stopped.get("stopped") is True, stopped

    after = await execute_tool("list_processes", {}, str(tmp_path))
    assert pid not in {row["pid"] for row in after["processes"]}


@pytest.mark.skipif(os.name != "nt", reason="detached spawn is Windows-only")
@pytest.mark.skipif(platform.system() != "Windows",
                    reason="spawn_detached replaces a Windows Job Object")
@pytest.mark.asyncio
async def test_list_tool_does_not_report_an_exited_process(tmp_path):
    """Negative control for liveness: a process that exited on its own is
    gone from the listing. Proves the list re-checks the OS instead of
    echoing registry rows back as alive."""
    # Exits immediately, so the row is recorded and then goes stale.
    cmd = " ".join([sys.executable, "-c", "pass"])
    r = await execute_tool("spawn_detached", {"command": cmd}, str(tmp_path))
    pid = r["pid"]

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _alive(pid):
        await asyncio.sleep(0.1)
    assert not _alive(pid), "control process never exited"

    listing = await execute_tool("list_processes", {}, str(tmp_path))
    assert pid not in {row["pid"] for row in listing["processes"]}, (
        "an exited process was still listed as a row"
    )


def test_panel_route_and_tool_share_one_payload():
    """The tool and GET /api/processes must not drift: both render
    processes_payload(), so the agent and the GUI see the same shape."""
    import inspect

    import backend.agent.detached as detached
    import backend.main as mainmod

    src = inspect.getsource(mainmod.api_processes)
    assert "processes_payload" in src
    payload = detached.processes_payload()
    assert set(payload) == {"processes", "known"}
    assert payload["known"] == len(payload["processes"])
