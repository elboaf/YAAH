"""Issue #770 stage 2: the /api/processes panel routes.

External behavior only: what the list reports, and what the stop route
refuses. The safety property is the load-bearing one -- stopping by PID kills
an arbitrary process, so a PID the backend never recorded must be refused.

These run on every host (not Windows-gated) on purpose: the routes and the
registry import cleanly on POSIX, and CI runs on Linux, so gating them would
leave the whole surface untested where CI can see it. On POSIX the registry
is simply empty, which is itself worth asserting.
"""
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

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
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

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
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

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
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
