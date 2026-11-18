"""Unit tests for the Interceptor manager (ticket t1-manager, #631).

The manager module's one seam to the world is the Interceptor CLI. These
tests fake that seam: YAAH_INTERCEPTOR_CLI points at a real temp file
(so the binary "exists") and `interceptor.subprocess.run` is patched to
return canned CompletedProcess objects (so no real subprocess runs). The
filesystem boundary (the skill links) is redirected via YAAH_SKILLS_PATH.

Per the ticket: tests fake the subprocess and filesystem boundaries and
assert status shapes/transitions -- not subprocess argument strings.
"""
import subprocess
from pathlib import Path

import pytest

from backend.agent import gitproc, interceptor


@pytest.fixture
def fake_skills_root(tmp_path, monkeypatch):
    """Redirect YAAH_SKILLS_PATH to a tmp dir; return it."""
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("YAAH_SKILLS_PATH", str(skills))
    return skills


def _fake_run(monkeypatch, version_stdout="", diagnose_output='{"problems": [], "blocked": []}',
              diagnose_rc=0):
    """Patch interceptor.subprocess.run so --version returns version_stdout
    and the diagnose command returns diagnose_output. The manager's own
    argv is passed through untouched; the harness only supplies canned
    outputs, never inspecting the arguments."""
    def fake_run(cmd, *a, **k):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=version_stdout, stderr="")
        return subprocess.CompletedProcess(cmd, diagnose_rc, stdout=diagnose_output, stderr="")

    monkeypatch.setattr(interceptor.subprocess, "run", fake_run)


def _install_cli(monkeypatch, exists: bool = True) -> Path:
    """Point YAAH_INTERCEPTOR_CLI at a real temp file (exists) or a path
    that does not (missing), and return that path."""
    cli_dir = Path(__import__("tempfile").mkdtemp(prefix="intcli-"))
    cli_path = cli_dir / "interceptor"
    if exists:
        cli_path.write_text("# fake\n", encoding="utf-8")
    monkeypatch.setenv(interceptor.INTERCEPTOR_CLI_ENV, str(cli_path))
    return cli_path


# --- installed / not installed ------------------------------------------


def test_not_installed_when_cli_missing(monkeypatch, fake_skills_root):
    """A missing CLI reads as 'not installed'; version None; skill links
    all 'not linked'; extension 'extension missing'."""
    _install_cli(monkeypatch, exists=False)
    status = interceptor.status()
    assert status["installed"] == "not installed"
    assert status["version"] is None
    assert status["cli"] is None
    assert all(v == "not linked" for v in status["skill_links"].values())
    assert status["skill_links_summary"] == "0/3"
    assert status["extension"] == "extension missing"


def test_installed_reports_version(monkeypatch, fake_skills_root):
    """A CLI that reports a version shows it in status."""
    _install_cli(monkeypatch, exists=True)
    _fake_run(monkeypatch, version_stdout="interceptor 2.1.6 (e14ad6a-dirty, 2026-10-31)",
              diagnose_output='{"problems": [], "blocked": []}')
    status = interceptor.status()
    assert status["installed"] == "installed"
    assert status["version"] == "2.1.6"
    assert status["cli"] is not None


def test_version_none_when_unparseable(monkeypatch, fake_skills_root):
    """A present CLI with an unparseable version reports installed but
    version None (best-effort metadata)."""
    _install_cli(monkeypatch, exists=True)
    _fake_run(monkeypatch, version_stdout="garbage output",
              diagnose_output='{"problems": [], "blocked": []}')
    status = interceptor.status()
    assert status["installed"] == "installed"
    assert status["version"] is None


def test_missing_cli_version_returns_none_without_raising(monkeypatch, fake_skills_root):
    """No CLI: version None and 'not installed', never an exception."""
    _install_cli(monkeypatch, exists=False)
    assert interceptor.installed_version() is None
    assert interceptor.installed_state() == "not installed"


def test_version_nonzero_rc_returns_none(monkeypatch, fake_skills_root):
    """A version command that fails (nonzero rc) but prints version-like
    stdout returns None -- the failure is honored, not misread as a version.

    This is the CodeRabbit hardening: installed_version checks returncode
    before parsing stdout."""
    _install_cli(monkeypatch, exists=True)
    def fake_run_failing(cmd, *a, **k):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 2,
                stdout="interceptor 9.9.9 (e14ad6a-dirty, 2026-10-31)", stderr="boom")
        return subprocess.CompletedProcess(cmd, 0,
            stdout='{"problems": [], "blocked": []}', stderr="")
    monkeypatch.setattr(interceptor.subprocess, "run", fake_run_failing)
    assert interceptor.installed_version() is None
    # Installation detection stays independent: still reported installed.
    assert interceptor.installed_state() == "installed"


def test_version_nonzero_rc_in_status(monkeypatch, fake_skills_root):
    """status() reports version None when --version fails with nonzero rc."""
    _install_cli(monkeypatch, exists=True)
    def fake_run_failing(cmd, *a, **k):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 1,
                stdout="interceptor 9.9.9", stderr="boom")
        return subprocess.CompletedProcess(cmd, 0,
            stdout='{"problems": [], "blocked": []}', stderr="")
    monkeypatch.setattr(interceptor.subprocess, "run", fake_run_failing)
    status = interceptor.status()
    assert status["version"] is None
    assert status["installed"] == "installed"


# --- extension state -----------------------------------------------------


def _ext(monkeypatch, diagnose_output='{"problems": [], "blocked": []}', diagnose_rc=0):
    _install_cli(monkeypatch, exists=True)
    _fake_run(monkeypatch,
              version_stdout="interceptor 2.1.6 (e14ad6a-dirty, 2026-10-31)",
              diagnose_output=diagnose_output, diagnose_rc=diagnose_rc)


def test_extension_ready_when_no_problems(monkeypatch, fake_skills_root):
    _ext(monkeypatch)
    assert interceptor.extension_state() == "ready"


def test_extension_missing_with_problems(monkeypatch, fake_skills_root):
    _ext(monkeypatch, diagnose_output='{"problems": [{"kind": "old_id"}], "blocked": []}')
    assert interceptor.extension_state() == "extension missing"


def test_extension_missing_when_blocked(monkeypatch, fake_skills_root):
    _ext(monkeypatch, diagnose_output='{"problems": [], "blocked": [{"kind": "not_loaded"}]}')
    assert interceptor.extension_state() == "extension missing"


def test_extension_missing_when_cli_missing(monkeypatch, fake_skills_root):
    _install_cli(monkeypatch, exists=False)
    assert interceptor.extension_state() == "extension missing"


def test_extension_missing_on_non_json(monkeypatch, fake_skills_root):
    _ext(monkeypatch, diagnose_output="not json at all")
    assert interceptor.extension_state() == "extension missing"


def test_extension_missing_on_failing_command(monkeypatch, fake_skills_root):
    _ext(monkeypatch, diagnose_output="", diagnose_rc=1)
    assert interceptor.extension_state() == "extension missing"


def test_extension_missing_on_empty_stdout(monkeypatch, fake_skills_root):
    _ext(monkeypatch, diagnose_output="", diagnose_rc=0)
    assert interceptor.extension_state() == "extension missing"


def test_extension_missing_on_null_object(monkeypatch, fake_skills_root):
    """A code-0 diagnosis of JSON null parses fine but has no fields; the
    AttributeError that would escape is prevented and it reads 'extension
    missing'. CodeRabbit finding (line 238)."""
    _ext(monkeypatch, diagnose_output="null")
    assert interceptor.extension_state() == "extension missing"


def test_extension_missing_on_json_array(monkeypatch, fake_skills_root):
    """A code-0 diagnosis of JSON [] parses to a list; data.get() would raise
    AttributeError, which is prevented and reads 'extension missing'.
    CodeRabbit finding (line 238)."""
    _ext(monkeypatch, diagnose_output="[]")
    assert interceptor.extension_state() == "extension missing"


def test_extension_missing_when_fields_not_lists(monkeypatch, fake_skills_root):
    """A diagnosis with problems as a non-list value (e.g. null/empty object)
    is NOT treated as evidence of readiness; it reads 'extension missing'.
    CodeRabbit finding (line 240)."""
    _ext(monkeypatch, diagnose_output='{"problems": null, "blocked": null}')
    assert interceptor.extension_state() == "extension missing"
    _ext(monkeypatch, diagnose_output='{"other": "value"}')
    assert interceptor.extension_state() == "extension missing"


def test_extension_ready_requires_empty_lists(monkeypatch, fake_skills_root):
    """Both fields present and empty lists => ready; any non-list => missing."""
    _ext(monkeypatch, diagnose_output='{"problems": [], "blocked": []}')
    assert interceptor.extension_state() == "ready"


# --- skill links ---------------------------------------------------------


def test_all_links_linked(monkeypatch, fake_skills_root):
    """When all three slots resolve to real directories, each is 'linked'
    and the summary is 3/3."""
    for slot in interceptor.LINK_SLOTS:
        (fake_skills_root / slot).mkdir()
    states = interceptor.skill_links_state()
    assert all(v == "linked" for v in states.values())
    assert interceptor.skill_links_summary() == "3/3"


def test_partial_links(monkeypatch, fake_skills_root):
    """Only 'research' linked -> 1/3."""
    (fake_skills_root / "research").mkdir()
    states = interceptor.skill_links_state()
    assert states == {"router": "not linked", "browser": "not linked", "research": "linked"}
    assert interceptor.skill_links_summary() == "1/3"


def test_no_links_when_skills_dir_missing(monkeypatch, tmp_path):
    """A missing skills dir (fresh install) reports every link 'not
    linked' and 0/3, never an exception."""
    monkeypatch.delenv("YAAH_SKILLS_PATH", raising=False)
    skills = tmp_path / "no-skills"
    monkeypatch.setattr(interceptor, "SKILLS_DIR", skills)
    states = interceptor.skill_links_state()
    assert all(v == "not linked" for v in states.values())
    assert interceptor.skill_links_summary() == "0/3"


def test_dangling_link_reads_not_linked(monkeypatch, fake_skills_root, tmp_path):
    """A dangling junction (Interceptor uninstalled externally) reads as
    'not linked', not an exception -- the external-uninstall case."""
    link = fake_skills_root / "router"
    target = tmp_path / "does-not-exist"
    try:
        import os
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink support on this host")
    assert interceptor.skill_links_state()["router"] == "not linked"


# --- manifest ------------------------------------------------------------


def test_manifest_reads_pinned_values(monkeypatch):
    """read_pinned_manifest returns the committed asset URL + SHA + version
    + license note."""
    manifest = interceptor.read_pinned_manifest()
    assert manifest is not None
    assert manifest["url"].endswith(".exe")
    assert manifest["sha256"] == manifest["sha256"].lower()
    assert len(manifest["sha256"]) == 64
    assert "Elastic License" in manifest["license"]


def test_manifest_none_when_absent(monkeypatch, tmp_path):
    """When no discovery candidate holds the manifest (or a found file is
    unreadable/invalid), read returns None, never an exception. The scan
    roots are redirected too: a repo checkout IS a valid packaged-layout
    candidate (cwd/backend), so isolation needs the whole scan pointed
    away (#758)."""
    monkeypatch.setattr(gitproc, "exe_dir", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(interceptor, "MANIFEST_PATH", tmp_path / "absent.json")
    assert interceptor.read_pinned_manifest() is None


def test_manifest_none_when_invalid_json(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(gitproc, "exe_dir", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(interceptor, "MANIFEST_PATH", bad)
    assert interceptor.read_pinned_manifest() is None


def test_manifest_found_in_packaged_layout(monkeypatch, tmp_path):
    """#758: the installed app ships the manifest as a Tauri resource at
    <exe>/_up_/backend/agent/ (the frozen one-file extraction dir carries
    no data files). When the beside-module anchor is absent, discovery
    must find the packaged copy via gitproc.bundle_dirs -- or the Install
    button can never verify a download."""
    packaged = tmp_path / "_up_" / "backend" / "agent"
    packaged.mkdir(parents=True)
    (packaged / "interceptor_manifest.json").write_text(
        '{"url": "https://example.com/x.exe", "sha256": "' + "a" * 64 + '"}',
        encoding="utf-8",
    )
    # The beside-module anchor must not exist in this scenario: point it
    # at a path that is not a file so only the packaged copy can match.
    monkeypatch.setattr(interceptor, "MANIFEST_PATH", tmp_path / "none.json")
    # Patch the scan's exe dir at its source so the packaged candidate
    # lands first (bundle_dirs reads gitproc.exe_dir() live).
    monkeypatch.setattr(interceptor.gitproc, "exe_dir", lambda: tmp_path)
    manifest = interceptor.read_pinned_manifest()
    assert manifest is not None
    assert manifest["url"] == "https://example.com/x.exe"
    assert manifest["sha256"] == "a" * 64


# --- status contract -----------------------------------------------------


def test_status_has_all_contract_keys(monkeypatch, fake_skills_root):
    """status() returns exactly the contract keys the API/UI/skill speak."""
    _ext(monkeypatch)
    _fake_run(monkeypatch, version_stdout="interceptor 2.1.6 (e14ad6a-dirty, 2026-10-31)",
              diagnose_output='{"problems": [], "blocked": []}')
    for slot in interceptor.LINK_SLOTS:
        (fake_skills_root / slot).mkdir()

    status = interceptor.status()
    assert set(status) == {
        "installed",
        "version",
        "skill_links",
        "skill_links_summary",
        "extension",
        "cli",
    }
    assert status["installed"] == "installed"
    assert status["version"] == "2.1.6"
    assert status["skill_links_summary"] == "3/3"
    assert status["extension"] == "ready"
    assert status["cli"] is not None


def test_status_when_not_installed(monkeypatch, fake_skills_root):
    """A full not-installed status: installed string 'not installed',
    version None, all links not linked, extension missing."""
    _install_cli(monkeypatch, exists=False)
    status = interceptor.status()
    assert status["installed"] == "not installed"
    assert status["version"] is None
    assert status["extension"] == "extension missing"
    assert status["skill_links_summary"] == "0/3"


# --- extension dir (t6-guided-panel, #636) --------------------------------


def test_extension_dir_reports_install_layout(monkeypatch, tmp_path):
    """The unpacked-extension folder is reported when the documented
    install dir holds one with a loadable manifest."""
    monkeypatch.setattr(interceptor, "_INSTALL_DIR_CANDIDATES", (str(tmp_path),))
    (tmp_path / "extension").mkdir()
    (tmp_path / "extension" / "manifest.json").write_text("{}", encoding="utf-8")
    ext = interceptor.extension_dir()
    assert ext == str(tmp_path / "extension")


def test_extension_dir_none_when_no_extension(monkeypatch, tmp_path):
    """No install dir (or an empty one): None -- the panel shows no path
    rather than a wrong one."""
    monkeypatch.setattr(interceptor, "_INSTALL_DIR_CANDIDATES", (str(tmp_path),))
    assert interceptor.extension_dir() is None


def test_extension_dir_none_when_manifest_missing(monkeypatch, tmp_path):
    """A folder named extension without a loadable manifest is not
    reported: the probe checks the manifest, not just the directory."""
    monkeypatch.setattr(interceptor, "_INSTALL_DIR_CANDIDATES", (str(tmp_path),))
    (tmp_path / "extension").mkdir()
    (tmp_path / "extension" / "background.js").write_text("//", encoding="utf-8")
    assert interceptor.extension_dir() is None


def test_extension_dir_never_spawns_a_subprocess(monkeypatch, tmp_path):
    """Pure filesystem probe: no CLI lookup, no diagnose run -- same
    constraint path_prepend_dir() is held to."""
    monkeypatch.setattr(interceptor, "_INSTALL_DIR_CANDIDATES", (str(tmp_path),))

    def _boom(cmd, *a, **k):
        raise AssertionError("extension_dir must not spawn a subprocess")

    monkeypatch.setattr(interceptor.subprocess, "run", _boom)
    monkeypatch.setenv(interceptor.INTERCEPTOR_CLI_ENV, str(tmp_path / "nowhere"))
    assert interceptor.extension_dir() is None
