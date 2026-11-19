"""Issue #770 stage 2: the /api/processes panel routes.

External behavior only: what the list reports, and what the stop route
refuses. The safety property is the load-bearing one -- stopping by PID kills
an arbitrary process, so a PID the backend never recorded must be refused.

These run on every host (not Windows-gated) on purpose: the routes and the
registry import cleanly on POSIX, and CI runs on Linux, so gating them would
leave the whole surface untested where CI can see it. On POSIX the registry
is simply empty, which is itself worth asserting.

IMPORTANT: every child spawned here uses ``start_new_session=(os.name != "nt")``
to mirror what spawn_detached does. On POSIX stop_process kills the recorded
PID's whole process GROUP (``os.killpg``), so a child spawned without its own
session shares pytest's group -- and stopping it would kill the test runner
itself. That is exactly what happened on CI: the "Run tests" step never
concluded and the job was killed.
"""
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    import backend.main as mainmod

    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    from backend.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_registry():
    """Never leave a recorded row (or a live child) behind."""
    import backend.agent.detached as detached

    yield
    for pid in list(detached._REGISTRY):
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    with detached._lock:
        detached._REGISTRY.clear()


def test_list_is_empty_and_never_claims_omniscience(client):
    """A fresh backend knows about no detached processes, and says so with an
    empty list rather than failing."""
    r = client.get("/api/processes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["processes"] == []
    assert body["known"] == 0


def test_list_reports_a_recorded_process_with_its_fields(client):
    """A recorded row reaches the panel with the fields it renders: pid,
    command, cwd, log_path, and a live `alive` flag."""
    import backend.agent.detached as detached

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         start_new_session=(os.name != "nt"))
    detached._record(
        proc.pid, command="echo panel", cwd="/tmp/panel",
        log_path="/tmp/panel.log", conversation_id=None,
    )
    try:
        body = client.get("/api/processes").json()
        rows = {row["pid"]: row for row in body["processes"]}
        assert proc.pid in rows, f"recorded row missing from {body}"
        row = rows[proc.pid]
        assert row["command"] == "echo panel"
        assert row["cwd"] == "/tmp/panel"
        assert row["log_path"] == "/tmp/panel.log"
        assert row["alive"] is True
    finally:
        proc.kill()


def test_list_prunes_a_dead_row(client):
    """A row whose process has exited must not be reported (story 14: stopped
    processes disappear from the list)."""
    import backend.agent.detached as detached

    proc = subprocess.Popen([sys.executable, "-c", "pass"],
                         start_new_session=(os.name != "nt"))
    pid = proc.pid
    proc.wait(timeout=10)
    detached._record(
        pid, command="echo gone", cwd="/tmp/gone",
        log_path="/tmp/gone.log", conversation_id=None,
    )
    body = client.get("/api/processes").json()
    assert all(row["pid"] != pid for row in body["processes"]), body


def test_stop_refuses_a_pid_the_registry_never_recorded(client):
    """The safety gate: stop-by-PID on an unrecorded PID is refused with a
    400, so the UI cannot read it as a successful stop."""
    r = client.post("/api/processes/stop", json={"pid": 999999})
    assert r.status_code == 400, r.text
    assert "no agent-spawned process" in r.json()["detail"]


def test_stop_kills_a_recorded_process_and_removes_the_row(client):
    """A recorded live process is actually stopped, and the row leaves the
    list afterwards."""
    import backend.agent.detached as detached

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                         start_new_session=(os.name != "nt"))
    detached._record(
        proc.pid, command="echo stoppable", cwd="/tmp/stoppable",
        log_path="/tmp/stoppable.log", conversation_id=None,
    )
    r = client.post("/api/processes/stop", json={"pid": proc.pid})
    assert r.status_code == 200, r.text
    assert r.json()["stopped"] is True

    # The row must be gone from the list the panel reads.
    for _ in range(20):
        if not detached._alive(proc.pid):
            break
        time.sleep(0.1)
    body = client.get("/api/processes").json()
    assert all(row["pid"] != proc.pid for row in body["processes"]), body


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_stop_never_kills_the_callers_own_process_group(client):
    """A stale/advisory row can name a process in OUR OWN process group (a
    child spawned without its own session). killpg on that group would kill
    the backend itself -- this is the regression: on CI the test runner was
    killed this way and the job died with the "Run tests" step never
    concluding.

    The guard: kill just that PID, never a group we belong to.
    """
    import backend.agent.detached as detached

    # Deliberately NO start_new_session: this child shares our group, which is
    # the dangerous shape the guard must survive.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    assert os.getpgid(proc.pid) == os.getpgid(0), "test setup: child is not in our group"
    detached._record(
        proc.pid, command="echo own-group", cwd="/tmp/own-group",
        log_path="/tmp/own-group.log", conversation_id=None,
    )
    try:
        r = client.post("/api/processes/stop", json={"pid": proc.pid})
        # We are still alive to assert -- that is the point.
        assert r.status_code == 200, r.text
        assert r.json()["stopped"] is True
        assert not detached._alive(proc.pid), "the target process should be dead"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_kill_tree_kills_pid_not_own_group(monkeypatch):
    """The POSIX self-kill guard, pinned cross-platform.

    A detached child gets its own session, so its group normally differs from
    ours. When the recorded PID's group MATCHES our own, the PID is not a
    detached child (the registry is advisory) and killpg would kill the
    backend itself -- on CI this killed the test runner and the job died with
    the "Run tests" step never concluding. The guard must kill the single PID.

    The os functions are injected, so this verifies the POSIX branch on
    Windows too, where it never executes natively.
    """
    import backend.agent.detached as detached

    calls: list[tuple[str, int]] = []
    # The target and "us" report the SAME group: the dangerous shape.
    monkeypatch.setattr(detached.os, "name", "posix")
    getpgid = lambda _p: 4242  # noqa: E731 - same group for both
    ok = detached._kill_tree(
        777,
        getpgid=getpgid,
        killpg=lambda pg, sig: calls.append(("killpg", pg)),
        kill=lambda p, sig: calls.append(("kill", p)),
    )
    assert ok is True
    assert ("killpg", 4242) not in calls, f"guard failed: killpg on our own group; {calls}"
    assert ("kill", 777) in calls, f"target PID not killed alone; {calls}"


def test_kill_tree_uses_killpg_for_a_foreign_group(monkeypatch):
    """The normal POSIX case still kills the whole group: a real detached
    child has its own session, so its group is NOT ours and the tree must die.
    Without this, the guard above could pass by never killing a group at all.
    """
    import backend.agent.detached as detached

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(detached.os, "name", "posix")
    # Target in group 9999; we are in 4242.
    getpgid = lambda p: 9999 if p == 777 else 4242  # noqa: E731
    ok = detached._kill_tree(
        777,
        getpgid=getpgid,
        killpg=lambda pg, sig: calls.append(("killpg", pg)),
        kill=lambda p, sig: calls.append(("kill", p)),
    )
    assert ok is True
    assert ("killpg", 9999) in calls, f"foreign group should be killed as a tree; {calls}"
    assert ("kill", 777) not in calls
