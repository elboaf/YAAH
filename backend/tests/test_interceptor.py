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

from backend.agent import interceptor


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
    """When MANIFEST_PATH is missing (or unreadable/invalid), read returns
    None, never an exception."""
    monkeypatch.setattr(interceptor, "MANIFEST_PATH", tmp_path / "absent.json")
    assert interceptor.read_pinned_manifest() is None


def test_manifest_none_when_invalid_json(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(interceptor, "MANIFEST_PATH", bad)
    assert interceptor.read_pinned_manifest() is None


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
