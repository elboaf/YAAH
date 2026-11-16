"""Unit tests for the Interceptor install flow (ticket t2-install, #632).

Per the ticket, tests fake the process boundary (the installer runs and the
download both go through interceptor.subprocess / module functions) and
assert the *filesystem effects* (temp-file cleanup, junction targets,
marker, no-write-on-failure) -- not argument strings.

The download, hash, installer run, and junction creation are faked by
replacing the module's own functions. The install temp dir is redirected so
no test writes into the real ~/.yaah. The real-junction tests are gated on
Windows (junctions are a Windows-only object); on non-Windows hosts they are
skipped with a reason.
"""
import os
import subprocess
from pathlib import Path

import pytest

from backend.agent import interceptor, interceptor_install as inst


# --- fixtures -----------------------------------------------------------


@pytest.fixture
def fake_install(monkeypatch, tmp_path):
    """Redirect the install temp dir to a tmp dir and return it, so no test
    writes into the real ~/.yaah. Also resets the marker path."""
    tmp = tmp_path / "installer_tmp"
    tmp.mkdir()
    monkeypatch.setattr(inst, "_INSTALL_TMP", tmp)
    monkeypatch.setattr(inst, "_DONE_MARKER_PATH", tmp / "interceptor_install_done.txt")
    return tmp


@pytest.fixture
def manifest(monkeypatch, tmp_path):
    """Point the manager's manifest at a tmp file with the pinned shape
    (url + sha256 matching _fake_hash's default digest)."""
    path = tmp_path / "interceptor_manifest.json"
    path.write_text(
        '{"url": "https://example/Interceptor.exe", '
        '"sha256": "%s", ' % ("ab" * 32)
        + '"license": "Elastic License 2.0", '
        '"version": "2.1.6"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(interceptor, "MANIFEST_PATH", path)
    return path


def _fake_download(monkeypatch, content: bytes = b"installer-bytes"):
    """Fake _download_asset to write bytes to a temp file and return that
    path (simulating curl writing a body). Returns a callable the caller can
    also use to simulate a failure."""

    def _dl(url):
        p = Path(__import__("tempfile").mkdtemp(prefix="dl-")) / "installer.exe"
        p.write_bytes(content)
        return p

    monkeypatch.setattr(inst, "_download_asset", _dl)
    return _dl


def _fake_hash(monkeypatch, digest: str = "ab" * 32):
    monkeypatch.setattr(inst, "_sha256", lambda _p: digest)


def _recorded_calls():
    return {"installer_run": False}


def _fake_installer(monkeypatch, rc: int = 0, calls=None):
    """Fake _run_installer to record that it ran and return the given rc."""
    calls = calls or _recorded_calls()

    def _run(exe, log):
        calls["installer_run"] = True
        log.write_text("installed", encoding="utf-8")
        return subprocess.CompletedProcess([str(exe)], rc, stdout="", stderr="")

    monkeypatch.setattr(inst, "_run_installer", _run)
    return calls


def _install_cli(monkeypatch, exists: bool = True):
    """Make the manager think Interceptor is already installed (or not).

    Patch the CLI *finder* directly -- a real Interceptor ships on the host
    (on PATH), so patching only the documented install-dir candidates would
    still be fooled by the PATH fallthrough.
    """
    if exists:
        monkeypatch.setattr(
            interceptor, "_find_interceptor_cli", lambda: "C:\\fake\\interceptor.exe"
        )
    else:
        monkeypatch.setattr(interceptor, "_find_interceptor_cli", lambda: None)


def _junctions(monkeypatch):
    """Fake _create_junctions to return the three link names -> pack paths."""
    monkeypatch.setattr(
        inst,
        "_create_junctions",
        lambda: {name: f"/packs/{name}" for name in interceptor.LINK_SLOTS},
    )


def _has_marker(fake_install) -> bool:
    return (fake_install / "interceptor_install_done.txt").exists()


# --- refusal / hash -----------------------------------------------------


def test_manifest_missing_refuses(monkeypatch, manifest, fake_install):
    manifest.unlink()  # absent manifest
    with pytest.raises(RuntimeError, match="manifest missing"):
        inst.install()
    assert not _has_marker(fake_install)


def test_manifest_missing_sha_refuses(monkeypatch, manifest, fake_install):
    manifest.write_text(
        '{"url": "https://example/Interceptor.exe"}', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="url or sha256"):
        inst.install()


def test_hash_mismatch_refuses_before_execution(
    monkeypatch, manifest, fake_install
):
    """A hash mismatch aborts before execution and leaves no side effects:
    nothing run, no marker, no junctions."""
    _fake_download(monkeypatch)
    _fake_hash(monkeypatch, digest="ff" * 32)  # wrong digest
    calls = _fake_installer(monkeypatch)
    _junctions(monkeypatch)

    with pytest.raises(RuntimeError, match="hash mismatch"):
        inst.install()

    assert not _has_marker(fake_install)
    assert calls["installer_run"] is False


def test_download_failure_leaves_no_state(monkeypatch, manifest, fake_install):
    """A download that writes nothing aborts before execution; no marker."""

    def _dl(url):
        raise RuntimeError("installer download failed (curl exit 22)")

    monkeypatch.setattr(inst, "_download_asset", _dl)
    _fake_hash(monkeypatch)
    _fake_installer(monkeypatch)
    _junctions(monkeypatch)

    with pytest.raises(RuntimeError, match="download failed"):
        inst.install()
    assert not _has_marker(fake_install)


# --- successful install -------------------------------------------------


def test_successful_install_writes_marker(monkeypatch, manifest, fake_install):
    _install_cli(monkeypatch, exists=False)
    _fake_download(monkeypatch)
    _fake_hash(monkeypatch)
    calls = _fake_installer(monkeypatch)
    _junctions(monkeypatch)

    res = inst.install()
    assert res["installed"] is True
    assert res["marker"] is True
    assert res["did_install"] is True  # CLI was missing, so the installer ran
    assert calls["installer_run"] is True
    assert _has_marker(fake_install)


def test_successful_install_skips_run_when_already_installed(
    monkeypatch, manifest, fake_install
):
    _install_cli(monkeypatch, exists=True)
    _fake_download(monkeypatch)
    _fake_hash(monkeypatch)
    calls = _fake_installer(monkeypatch)
    _junctions(monkeypatch)

    res = inst.install()
    assert res["did_install"] is False  # CLI present -> installer run skipped
    assert calls["installer_run"] is False
    assert res["marker"] is True
    assert _has_marker(fake_install)


# --- idempotency --------------------------------------------------------


def test_idempotent_rerun_reports_nothing_rerun(
    monkeypatch, manifest, fake_install
):
    """A prior successful install (marker + CLI present) is reported, not
    repeated: the download, installer run, and junctions are all skipped."""
    _install_cli(monkeypatch, exists=True)
    (fake_install / "interceptor_install_done.txt").write_text("done", encoding="utf-8")

    def _boom(_url):
        raise AssertionError("download must not run on idempotent re-run")

    def _boom_junctions():
        raise AssertionError("junctions must not run on idempotent re-run")

    monkeypatch.setattr(inst, "_download_asset", _boom)
    monkeypatch.setattr(inst, "_run_installer", _boom)
    monkeypatch.setattr(inst, "_create_junctions", _boom_junctions)

    res = inst.install()
    assert res["did_install"] is False
    assert res["note"] == "already installed; nothing re-run"


# --- real junctions (Windows only) --------------------------------------


def test_is_junction_distinguishes_from_real_dir(tmp_path):
    """The reparse-point discriminator separates a junction from a plain
    directory (the Windows-only fact that os.path cannot)."""
    if os.sys.platform != "win32":
        pytest.skip("junctions are Windows-only")

    packs = tmp_path / "packs"
    (packs / "real").mkdir(parents=True)

    junction = tmp_path / "junction"
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f'New-Item -ItemType Junction -Path {str(junction)!r} -Target {str(packs/"real")!r}'],
        check=True, timeout=30,
    )
    real = packs / "real"
    assert inst._is_junction(junction) is True
    assert inst._is_junction(real) is False
    assert inst._is_junction(tmp_path / "missing") is False


def test_real_junctions_created_on_install(monkeypatch, manifest, tmp_path, fake_install):
    """A real install creates three junctions under the skills dir pointing
    at the installed packs, and writes the done-marker."""
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("YAAH_SKILLS_PATH", str(skills))
    # The CLI must look absent so the installer actually runs (a real host
    # may ship Interceptor on PATH); redirect the install tmp so no marker
    # or log leaks into the real ~/.yaah.
    _install_cli(monkeypatch, exists=False)

    packs = tmp_path / "Programs" / "Interceptor" / "skills"
    for name in ("interceptor", "interceptor-browser", "interceptor-research"):
        (packs / name).mkdir(parents=True)
    cli_dir = packs.parent  # .../Programs/Interceptor
    (cli_dir / "interceptor.exe").write_text("# fake", encoding="utf-8")
    monkeypatch.setattr(interceptor, "_INSTALL_DIR_CANDIDATES", (str(cli_dir),))

    _fake_download(monkeypatch)
    _fake_hash(monkeypatch)
    _fake_installer(monkeypatch)

    res = inst.install()

    for name in interceptor.LINK_SLOTS:
        link = skills / name
        assert link.is_dir(), f"junction {name} not created"
        assert inst._is_junction(link), f"{name} is not a junction"
    assert res["marker"] is True
    assert _has_marker(tmp_path / "installer_tmp")


# --- remove_links ---------------------------------------------------------


def test_remove_links_deletes_junctions_only(monkeypatch, manifest, tmp_path, fake_install):
    """remove_links() deletes only the junctions YAAH created; a pre-existing
    real directory at a slot is left untouched (the acceptance criterion)."""
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("YAAH_SKILLS_PATH", str(skills))

    packs = tmp_path / "packs"
    packs.mkdir()
    real_target = packs / "keep_me"
    real_target.mkdir()

    # router -> a real junction (YAAH-created); browser -> a real directory
    # (the user's own; must survive); research -> absent (nothing to do).
    router = skills / "router"
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f'New-Item -ItemType Junction -Path {str(router)!r} -Target {str(real_target)!r}'],
        check=True, timeout=30,
    )
    browser = skills / "browser"
    browser.mkdir()

    removed = inst.remove_links()

    assert str(router) in removed, "junction was not removed"
    assert not router.exists(), "junction still exists"
    assert real_target.is_dir(), "junction target (a real dir) was removed"
    assert browser.is_dir(), "real directory slot was removed"
