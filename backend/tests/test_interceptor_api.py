"""Contract tests for the Interceptor API routes (ticket t3-api, #633).

The routes are thin wrappers over the manager modules (interceptor.status,
interceptor_install.install/remove_links), so the contract asserted here
is wiring, not logic: each route calls its manager seam, returns the
answer as documented, and the install route maps the flow's expected
failure classes to their own HTTP codes instead of a raw 500. The
manager seams are faked exactly the way test_interceptor_install.py
fakes them (patch the module attribute); all behavior behind the seam is
owned by the manager modules' own unit tests.
"""
import pytest
from fastapi.testclient import TestClient

from backend.agent import config as cfgmod
from backend.agent import interceptor
from backend.agent import interceptor_install as inst
from backend.db import database


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Isolated config + DB; the real app under TestClient."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test_intc.db")
    monkeypatch.setattr(database, "_migrated", set())
    import backend.main as mainmod

    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    monkeypatch.setattr(mainmod, "save_config", cfgmod.save_config)
    from backend.main import app

    with TestClient(app) as c:
        yield c


# --- manager seams (the routes' only inputs) -------------------------------


def _installed_status() -> dict:
    return {
        "installed": "installed",
        "version": "2.1.6",
        "skill_links": {name: "linked" for name in interceptor.LINK_SLOTS},
        "skill_links_summary": "3/3",
        "extension": "ready",
        "cli": "C:\\fake\\interceptor.exe",
    }


def _not_installed_status() -> dict:
    return {
        "installed": "not installed",
        "version": None,
        "skill_links": {name: "not linked" for name in interceptor.LINK_SLOTS},
        "skill_links_summary": "0/3",
        "extension": "extension missing",
        "cli": None,
    }


def _fake_status(monkeypatch, payload: dict | None = None) -> dict:
    calls = {"status": 0}

    def _status():
        calls["status"] += 1
        return dict(payload if payload is not None else _installed_status())

    monkeypatch.setattr(interceptor, "status", _status)
    return calls


def _fake_manifest(monkeypatch) -> dict:
    manifest = {
        "name": "Interceptor",
        "version": "2.1.6",
        "url": "https://example/Interceptor-Browser-2.1.6-windows-x64.exe",
        "sha256": "cd" * 32,
        "license": "Elastic License 2.0",
    }
    monkeypatch.setattr(interceptor, "read_pinned_manifest", lambda: dict(manifest))
    return manifest


def _fake_install(monkeypatch, result: dict) -> dict:
    calls = {"install": 0}

    def _install():
        calls["install"] += 1
        return dict(result)

    monkeypatch.setattr(inst, "install", _install)
    return calls


def _failing_install(monkeypatch, exc: RuntimeError) -> dict:
    calls = {"install": 0}

    def _install():
        calls["install"] += 1
        raise exc

    monkeypatch.setattr(inst, "install", _install)
    return calls


def _fake_remove_links(monkeypatch, result: dict) -> dict:
    calls = {"remove_links": 0}

    def _remove():
        calls["remove_links"] += 1
        return dict(result)

    monkeypatch.setattr(inst, "remove_links", _remove)
    return calls


# --- GET /api/interceptor/status -------------------------------------------


def test_status_returns_manager_contract_verbatim(client, monkeypatch):
    payload = _installed_status()
    _fake_status(monkeypatch, payload)
    r = client.get("/api/interceptor/status")
    assert r.status_code == 200, r.text
    body = r.json()
    # Verbatim: the route adds only `pinned`, never reshapes the contract.
    assert body["installed"] == payload["installed"]
    assert body["version"] == payload["version"]
    assert body["skill_links"] == payload["skill_links"]
    assert body["skill_links_summary"] == payload["skill_links_summary"]
    assert body["extension"] == payload["extension"]
    assert body["cli"] == payload["cli"]


def test_status_reports_not_installed_shape(client, monkeypatch):
    payload = _not_installed_status()
    _fake_status(monkeypatch, payload)
    body = client.get("/api/interceptor/status").json()
    assert body["installed"] == "not installed"
    assert body["version"] is None
    assert body["skill_links_summary"] == "0/3"
    assert body["extension"] == "extension missing"
    assert body["cli"] is None


def test_status_includes_pinned_manifest_info(client, monkeypatch):
    manifest = _fake_manifest(monkeypatch)
    _fake_status(monkeypatch)
    body = client.get("/api/interceptor/status").json()
    assert body["pinned"] == manifest


def test_status_get_has_no_install_side_effect(client, monkeypatch):
    _fake_status(monkeypatch)
    install_calls = _fake_install(monkeypatch, {"installed": True})
    client.get("/api/interceptor/status")
    # A status poll must never trigger the install flow.
    assert install_calls["install"] == 0


# --- POST /api/interceptor/install -----------------------------------------


def test_install_runs_flow_and_returns_post_install_status(client, monkeypatch):
    install_calls = _fake_install(
        monkeypatch,
        {
            "installed": True,
            "did_install": True,
            "junctions": {name: f"/packs/{name}" for name in interceptor.LINK_SLOTS},
            "marker": True,
        },
    )
    status_calls = _fake_status(monkeypatch)
    r = client.post("/api/interceptor/install")
    assert r.status_code == 200, r.text
    body = r.json()
    assert install_calls["install"] == 1
    # The flow's own result keys surface unchanged...
    assert body["installed"] is True
    assert body["did_install"] is True
    assert body["marker"] is True
    # ...plus the post-install status under `status`.
    assert body["status"]["installed"] == "installed"
    assert status_calls["status"] == 1


def test_install_rerun_reports_did_install_false(client, monkeypatch):
    _fake_install(
        monkeypatch,
        {
            "installed": True,
            "did_install": False,
            "junctions": {},
            "marker": True,
            "note": "already installed; junctions verified/repaired, nothing re-run",
        },
    )
    _fake_status(monkeypatch)
    body = client.post("/api/interceptor/install").json()
    # Safe to click twice: the second click is a no-op repair pass, and
    # the UI can tell the two apart by did_install.
    assert body["installed"] is True
    assert body["did_install"] is False


def test_install_hash_mismatch_maps_to_502(client, monkeypatch):
    calls = _failing_install(
        monkeypatch,
        RuntimeError(
            "installer hash mismatch: expected ab, got cd. "
            "Aborting before execution -- nothing installed, nothing run."
        ),
    )
    r = client.post("/api/interceptor/install")
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "hash_mismatch"
    assert "nothing was run or installed" in detail["message"]
    assert "hash mismatch" in detail["message"]  # the flow's own text rides along
    assert calls["install"] == 1


def test_install_download_failure_maps_to_503(client, monkeypatch):
    _failing_install(
        monkeypatch,
        RuntimeError("installer download failed (curl exit 22): The requested URL returned error: 404"),
    )
    r = client.post("/api/interceptor/install")
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "download_failed"
    assert "download failed" in detail["message"]


def test_install_installer_failure_maps_to_500_with_log_hint(client, monkeypatch):
    _failing_install(
        monkeypatch,
        RuntimeError("installer exited 1625: this account does not have permission"),
    )
    r = client.post("/api/interceptor/install")
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "installer_failed"
    assert "installer.log" in detail["message"]
    assert "installer exited 1625" in detail["message"]


def test_install_slot_conflict_maps_to_409(client, monkeypatch):
    _failing_install(
        monkeypatch,
        RuntimeError("conflict at skill slot C:\\skills\\router"),
    )
    r = client.post("/api/interceptor/install")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "slot_conflict"


def test_install_unexpected_failure_stays_500_with_flow_text(client, monkeypatch):
    _failing_install(
        monkeypatch,
        RuntimeError("interceptor skills root not found; nothing to junction to"),
    )
    r = client.post("/api/interceptor/install")
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "install_failed"
    # Never a bare traceback: the flow's human-readable text is the message.
    assert detail["message"] == "interceptor skills root not found; nothing to junction to"


# --- DELETE /api/interceptor/links -----------------------------------------


def test_delete_links_reports_removed_and_skipped(client, monkeypatch):
    calls = _fake_remove_links(
        monkeypatch,
        {
            "removed": ["C:\\skills\\router", "C:\\skills\\browser", "C:\\skills\\research"],
            "skipped": {},
        },
    )
    r = client.delete("/api/interceptor/links")
    assert r.status_code == 200, r.text
    body = r.json()
    assert calls["remove_links"] == 1
    assert body["removed"] == [
        "C:\\skills\\router",
        "C:\\skills\\browser",
        "C:\\skills\\research",
    ]
    assert body["skipped"] == {}


def test_delete_links_surfaces_skipped_entries_verbatim(client, monkeypatch):
    result = {
        "removed": ["C:\\skills\\router"],
        "skipped": {
            "browser": "not a link (real directory or absent)",
            "research": "no ownership record for this slot",
        },
    }
    _fake_remove_links(monkeypatch, result)
    body = client.delete("/api/interceptor/links").json()
    # Verbatim: what was left in place and why, unreshaped.
    assert body == result
