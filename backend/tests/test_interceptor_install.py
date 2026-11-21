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
import sys
from pathlib import Path

import pytest

from backend.agent import gitproc, interceptor, interceptor_install as inst


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
    (url + sha256 matching _fake_hash's default digest).

    Redirects the packaged-layout scan too (#758): read_pinned_manifest
    walks gitproc.bundle_dirs candidates before the MANIFEST_PATH anchor,
    and a repo checkout is itself a live cwd/backend candidate -- without
    this, install tests would read the real committed manifest."""
    path = tmp_path / "interceptor_manifest.json"
    path.write_text(
        '{"url": "https://example/Interceptor.exe", '
        '"sha256": "%s", ' % ("ab" * 32)
        + '"license": "Elastic License 2.0", '
        '"version": "2.1.6"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(gitproc, "exe_dir", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
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
    """Fake _create_junctions to return the three link names -> pack paths
    in the {"junctions": {...}, "occupied": {}} shape the caller reads."""
    monkeypatch.setattr(
        inst,
        "_create_junctions",
        lambda: {
            "junctions": {name: f"/packs/{name}" for name in interceptor.LINK_SLOTS},
            "occupied": {},
        },
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
    """A download that writes partial bytes then fails aborts before
    execution and cleans up the partial: no marker, no installer run."""
    def _run(cmd, *a, **k):
        idx = cmd.index("--output")
        Path(cmd[idx + 1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(cmd, 22, stdout="", stderr="404")

    monkeypatch.setattr(inst.subprocess, "run", _run)
    _fake_hash(monkeypatch)
    _fake_installer(monkeypatch)
    _junctions(monkeypatch)

    with pytest.raises(RuntimeError, match="download failed"):
        inst.install()
    # The partial file _download_asset() wrote must be cleaned up, so this
    # exercises the real cleanup path instead of a fake that raises first.
    assert not (fake_install / "installer.exe").exists(), \
        "_download_asset did not delete the partial download"
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


def test_idempotent_rerun_repairs_missing_junction(
    monkeypatch, manifest, fake_install
):
    """A prior successful install (marker + CLI present) re-runs nothing --
    no download, no installer -- but the repair-only junction pass still
    runs: a slot deleted or re-pointed since the last install is repaired,
    never silently left dangling (the ticket's repairs/reports criterion)."""
    _install_cli(monkeypatch, exists=True)
    (fake_install / "interceptor_install_done.txt").write_text("done", encoding="utf-8")

    def _boom_download(_url):
        raise AssertionError("download must not run on idempotent re-run")

    def _boom_installer(exe, log):
        raise AssertionError("installer must not run on idempotent re-run")

    monkeypatch.setattr(inst, "_download_asset", _boom_download)
    monkeypatch.setattr(inst, "_run_installer", _boom_installer)

    calls = {"junctions": 0}

    def _fake_repair():
        calls["junctions"] += 1
        return {
            "junctions": {name: f"/packs/{name}" for name in interceptor.LINK_SLOTS},
            "occupied": {},
        }

    monkeypatch.setattr(inst, "_create_junctions", _fake_repair)

    res = inst.install()
    assert res["did_install"] is False
    assert res["marker"] is True
    assert calls["junctions"] == 1, "repair pass must run on idempotent re-run"
    assert "repaired" in res["note"]


def test_idempotent_rerun_reports_when_packs_root_missing(
    monkeypatch, manifest, fake_install
):
    """Marker + CLI present but the packs root is gone (CLI resolves via
    PATH, install dir wiped): the re-click reports instead of failing."""
    _install_cli(monkeypatch, exists=True)
    (fake_install / "interceptor_install_done.txt").write_text("done", encoding="utf-8")

    def _no_root():
        raise RuntimeError("interceptor skills root not found; nothing to junction to")

    monkeypatch.setattr(inst, "_create_junctions", _no_root)

    res = inst.install()
    assert res["installed"] is True
    assert res["did_install"] is False
    assert res["marker"] is True
    assert "nothing re-run" in res["note"]


# --- real junctions (Windows only) --------------------------------------


def test_is_junction_distinguishes_from_real_dir(tmp_path):
    """The reparse-point discriminator separates a junction from a plain
    directory (the Windows-only fact that os.path cannot)."""
    if sys.platform != "win32":
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


def test_create_junctions_reports_bundled_skill_dir_as_occupied(monkeypatch, manifest, tmp_path):
    """#775 acceptance: a slot held by a real directory that is NOT a link
    (YAAH's own bundled 'research' skill colliding with the 'research'
    slot) is reported as 'occupied', never advertised as a junction target.

    `_create_junctions` is pure-stdlib path logic (only `_create_junction`
    itself shells out), so this runs everywhere and needs no real junctions.
    """
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("YAAH_SKILLS_PATH", str(skills))

    packs = tmp_path / "Programs" / "Interceptor" / "skills"
    for name in ("interceptor", "interceptor-browser", "interceptor-research"):
        (packs / name).mkdir(parents=True)
    monkeypatch.setattr(
        interceptor, "_INSTALL_DIR_CANDIDATES", (str(packs.parent),)
    )

    # 'research' is a real directory (the colliding bundled skill), not a
    # link. router/browser resolve to real packs and become junctions.
    (skills / "research").mkdir()

    # _create_junction shells out to PowerShell (a junction is a
    # Windows-only object), so faking the leaf lets this pure-path-logic
    # test run on Linux CI without a real junction -- matching the
    # test_symlink_slot_repair_via_seam pattern below. The report shape is
    # what we assert; the junction leaf itself is not under test here.
    created = []
    monkeypatch.setattr(
        inst,
        "_create_junction",
        lambda target, link: created.append((str(link), target)),
    )

    result = inst._create_junctions()

    # The bundled-skill slot is NOT a junction target...
    assert "research" not in result["junctions"]
    assert "research" in result["occupied"]
    assert result["occupied"]["research"] == str((skills / "research").resolve())
    # ...and the two genuine slots still get junction targets.
    assert result["junctions"].get("router") == str((packs / "interceptor").resolve())
    assert result["junctions"].get("browser") == str((packs / "interceptor-browser").resolve())
    # On a non-Windows host the junctions are not actually created, but the
    # report shape is still correct.



def test_real_junctions_created_on_install(monkeypatch, manifest, tmp_path, fake_install):
    """A real install creates three junctions under the skills dir pointing
    at the installed packs, and writes the done-marker."""
    if sys.platform != "win32":
        pytest.skip("junctions require Windows")
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


def test_remove_links_deletes_only_owned_junctions(
    monkeypatch, manifest, tmp_path, fake_install
):
    """remove_links() deletes only junctions the ownership record vouches
    for: a real directory at a slot and a link with no ownership record are
    left untouched (the acceptance criterion)."""
    if sys.platform != "win32":
        pytest.skip("junctions require Windows")
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("YAAH_SKILLS_PATH", str(skills))

    packs = tmp_path / "packs"
    packs.mkdir()
    real_target = packs / "keep_me"
    real_target.mkdir()

    # router -> a real junction, recorded as YAAH's (must be removed);
    # browser -> a real directory (the user's own; must survive);
    # research -> a foreign junction with NO ownership record (must survive).
    router = skills / "router"
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f'New-Item -ItemType Junction -Path {str(router)!r} -Target {str(real_target)!r}'],
        check=True, timeout=30,
    )
    browser = skills / "browser"
    browser.mkdir()
    foreign = skills / "research"
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f'New-Item -ItemType Junction -Path {str(foreign)!r} -Target {str(packs)!r}'],
        check=True, timeout=30,
    )
    inst._record_junction_ownership({"router": str(real_target.resolve())})

    result = inst.remove_links()

    assert result["removed"] == [str(router)], "owned junction was not removed"
    assert not router.exists(), "owned junction still exists"
    assert real_target.is_dir(), "junction target (a real dir) was removed"
    assert browser.is_dir(), "real directory slot was removed"
    assert foreign.is_dir(), "foreign (unrecorded) junction was removed"
    assert result["skipped"]["browser"] == "not a link (real directory or absent)"
    assert result["skipped"]["research"] == "no ownership record for this slot"


def test_remove_links_leaves_repointed_junction(
    monkeypatch, manifest, tmp_path, fake_install
):
    """A recorded slot whose junction has been re-pointed elsewhere is left
    alone: the ownership record vouches for the recorded target only."""
    if sys.platform != "win32":
        pytest.skip("junctions require Windows")
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("YAAH_SKILLS_PATH", str(skills))

    packs = tmp_path / "packs"
    packs.mkdir()
    recorded = packs / "recorded"
    recorded.mkdir()
    elsewhere = packs / "elsewhere"
    elsewhere.mkdir()

    router = skills / "router"
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f'New-Item -ItemType Junction -Path {str(router)!r} -Target {str(elsewhere)!r}'],
        check=True, timeout=30,
    )
    inst._record_junction_ownership({"router": str(recorded.resolve())})

    result = inst.remove_links()

    assert result["removed"] == [], "re-pointed junction was removed"
    assert router.is_dir(), "re-pointed junction did not survive"
    assert result["skipped"]["router"] == "link exists but points elsewhere than recorded"


def test_remove_links_without_record_removes_nothing(
    monkeypatch, manifest, tmp_path, fake_install
):
    """No ownership manifest -> nothing is removed (foreign-junction guard):
    without the record YAAH cannot prove a link is its own."""
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("YAAH_SKILLS_PATH", str(skills))
    (skills / "router").mkdir()  # entry type irrelevant: no record -> no removal

    result = inst.remove_links()

    assert result["removed"] == []
    assert (skills / "router").is_dir()
    # Every slot is skipped with a reason; none was removed on type alone.
    assert set(result["skipped"]) == set(interceptor.LINK_SLOTS)
    assert all(result["skipped"].values())


def test_ownership_manifest_round_trip_windows_path(monkeypatch, fake_install):
    """json.dumps escapes Windows backslashes so _read_junction_ownership()
    restores the exact target the round trip wrote (the json finding)."""
    win_target = r"C:\Program Files\Interceptor\packs"
    inst._record_junction_ownership({"router": win_target})
    assert inst._read_junction_ownership().get("router") == win_target


# --- symlink-at-slot repair (CodeRabbit round 2) -------------------------


def _can_create_dir_symlink(tmp_path) -> bool:
    """True when this host allows creating a directory symlink (Windows
    often denies it without admin/dev-mode, so tests skip, not fail)."""
    try:
        probe = tmp_path / "_probe_link"
        probe_dir = tmp_path / "_probe_dir"
        probe_dir.mkdir()
        os.symlink(str(probe_dir), str(probe), target_is_directory=True)
        probe.unlink()
        return True
    except (OSError, NotImplementedError, PermissionError):
        return False


def _make_symlink(link: Path, target: Path) -> None:
    os.symlink(str(target), str(link), target_is_directory=True)


def test_symlink_slot_gets_repaired(monkeypatch, manifest, tmp_path, fake_install):
    """A directory SYMLINK at a slot is a repairable link, not a preserved
    real directory: a mis-pointed one is replaced to point at the pack
    (is_dir() follows symlinks, so the real-dir guard must exclude them)."""
    if sys.platform != "win32":
        pytest.skip("junctions require Windows")
    if not _can_create_dir_symlink(tmp_path):
        pytest.skip("no symlink privilege on this host")
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("YAAH_SKILLS_PATH", str(skills))

    packs = tmp_path / "Programs" / "Interceptor" / "skills"
    for name in ("interceptor", "interceptor-browser", "interceptor-research"):
        (packs / name).mkdir(parents=True)
    monkeypatch.setattr(
        interceptor, "_INSTALL_DIR_CANDIDATES", (str(packs.parent),)
    )

    # router: correct junction (untouched); browser: MIS-POINTED symlink
    # (must be replaced); research: real dir (must be preserved).
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    router = skills / "router"
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f'New-Item -ItemType Junction -Path {str(router)!r} -Target {str(packs / "interceptor")!r}'],
        check=True, timeout=30,
    )
    _make_symlink(skills / "browser", elsewhere)
    (skills / "research").mkdir()

    junction_result = inst._create_junctions()
    junctions = junction_result["junctions"]

    assert inst._is_junction(router), "correct junction should be left alone"
    browser_link = skills / "browser"
    assert inst._is_junction(browser_link), "mis-pointed symlink not replaced"
    assert browser_link.resolve() == (packs / "interceptor-browser").resolve()
    assert (skills / "research").is_dir() and not inst._is_junction(skills / "research")
    assert junctions["browser"] == str((packs / "interceptor-browser").resolve())


def test_rerun_reclaims_ownership_after_manifest_wiped(
    monkeypatch, manifest, tmp_path, fake_install
):
    """Rerun with marker + CLI present but the ownership manifest gone:
    the repair pass re-records ownership so remove_links() can vouch for
    the links again."""
    if sys.platform != "win32":
        pytest.skip("junctions require Windows")
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("YAAH_SKILLS_PATH", str(skills))

    packs = tmp_path / "Programs" / "Interceptor" / "skills"
    for name in ("interceptor", "interceptor-browser", "interceptor-research"):
        (packs / name).mkdir(parents=True)
    monkeypatch.setattr(
        interceptor, "_INSTALL_DIR_CANDIDATES", (str(packs.parent),)
    )
    _install_cli(monkeypatch, exists=True)
    (fake_install / "interceptor_install_done.txt").write_text("done", encoding="utf-8")

    # First pass creates the junctions; wipe the ownership manifest;
    # rerun must re-record it (this host cannot create symlinks, so
    # junctions cover all three slots -- the manifest-wipe case holds).
    def _noop_download(_url):
        raise AssertionError("download must not run on rerun")

    monkeypatch.setattr(inst, "_download_asset", _noop_download)
    first = inst.install()  # junctions created, ownership recorded
    assert first["did_install"] is False
    inst._record_junction_ownership({})  # simulate the manifest being lost

    res = inst.install()

    assert "repaired" in res["note"]
    assert inst._read_junction_ownership(), "ownership manifest not restored"
    for name in interceptor.LINK_SLOTS:
        assert (skills / name).is_dir()


def test_preserved_real_dir_stays_unclaimed(monkeypatch, manifest, tmp_path, fake_install):
    """A slot preserved as the user's real directory is never recorded as
    YAAH-owned, so remove_links() can never delete the user's folder."""
    if sys.platform != "win32":
        pytest.skip("junctions require Windows")
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("YAAH_SKILLS_PATH", str(skills))

    packs = tmp_path / "Programs" / "Interceptor" / "skills"
    for name in ("interceptor", "interceptor-browser", "interceptor-research"):
        (packs / name).mkdir(parents=True)
    monkeypatch.setattr(
        interceptor, "_INSTALL_DIR_CANDIDATES", (str(packs.parent),)
    )
    _install_cli(monkeypatch, exists=True)
    (fake_install / "interceptor_install_done.txt").write_text("done", encoding="utf-8")
    (skills / "research").mkdir()  # the user's own dir at the slot

    inst.install()

    ownership = inst._read_junction_ownership()
    assert "research" not in ownership, "user's real dir was claimed as YAAH's"
    assert "router" in ownership and "browser" in ownership


def test_symlink_slot_repair_via_seam(monkeypatch, manifest, tmp_path, fake_install):
    """Same repair path as test_symlink_slot_gets_repaired, but with the
    powershell leaf faked so it runs wherever directory symlinks are
    creatable (Linux CI included): a mis-pointed dir symlink at a slot must
    be removed and the slot recreated, never preserved as a real dir."""
    if sys.platform == "win32" and not _can_create_dir_symlink(tmp_path):
        pytest.skip("no symlink privilege on this host")
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("YAAH_SKILLS_PATH", str(skills))

    packs = tmp_path / "Programs" / "Interceptor" / "skills"
    for name in ("interceptor", "interceptor-browser", "interceptor-research"):
        (packs / name).mkdir(parents=True)
    monkeypatch.setattr(
        interceptor, "_INSTALL_DIR_CANDIDATES", (str(packs.parent),)
    )

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    browser = skills / "browser"
    _make_symlink(browser, elsewhere)  # mis-pointed dir symlink at the slot

    created = []
    monkeypatch.setattr(
        inst, "_create_junction",
        lambda target, link: created.append(str(link)),
    )

    junction_result = inst._create_junctions()
    junctions = junction_result["junctions"]

    assert not os.path.lexists(str(browser)), "mis-pointed symlink survived"
    assert str(browser) in created, "slot was not recreated via the leaf"
    assert junctions["browser"] == str((packs / "interceptor-browser").resolve())
