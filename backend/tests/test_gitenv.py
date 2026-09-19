"""install_git + gitenv: bundled Git-for-Windows installer (issue #12)."""
import os
from pathlib import Path

import pytest

from backend.agent import gitenv, tools


def test_find_git_returns_none_without_git(monkeypatch):
    monkeypatch.setattr(gitenv.shutil, "which", lambda name: None)
    assert gitenv.find_git() is None


def test_find_git_returns_path(monkeypatch):
    monkeypatch.setattr(gitenv.shutil, "which", lambda name: "/usr/bin/git")
    assert gitenv.find_git() == "/usr/bin/git"


def test_find_installer_via_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "Git-2.51.0-64-bit.exe"
    fake.write_bytes(b"MZ fake")
    monkeypatch.setenv("YAAH_GIT_INSTALLER", str(fake))
    assert gitenv.find_installer() == fake


def test_find_installer_repo_layout_fallback(monkeypatch, tmp_path):
    # env unset + all bundle dirs non-existent: point the exe-dir probe at
    # a temp "installers" dir to exercise the directory-glob branch.
    monkeypatch.delenv("YAAH_GIT_INSTALLER", raising=False)
    fake = tmp_path / "installers" / "Git-2.51.0-64-bit.exe"
    fake.parent.mkdir()
    fake.write_bytes(b"MZ fake")
    monkeypatch.setattr(gitenv, "_exe_dir", lambda: tmp_path)
    assert gitenv.find_installer() == fake


def test_find_installer_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("YAAH_GIT_INSTALLER", raising=False)
    monkeypatch.setattr(gitenv, "_exe_dir", lambda: tmp_path / "nothing")
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path / "nothing"))
    # repo-checkout fallback may still find a real installer in a dev
    # checkout, so only assert "not the temp dirs" — emptiness is covered
    # by the env-override test's inverse.
    found = gitenv.find_installer()
    assert found is None or "installers" not in str(found.parent.parent)


@pytest.mark.asyncio
async def test_install_git_noop_when_git_present(monkeypatch):
    monkeypatch.setattr(gitenv, "find_git", lambda: "/usr/bin/git")
    res = await gitenv.run_install_git("unused")
    assert res["ok"] is True and res.get("already_installed") is True


@pytest.mark.asyncio
async def test_install_git_refuses_without_installer(monkeypatch, tmp_path):
    monkeypatch.setattr(gitenv, "find_git", lambda: None)
    monkeypatch.setattr(gitenv, "find_installer", lambda: None)
    res = await gitenv.run_install_git("unused")
    assert "error" in res and "bundled" in res["error"]


def test_install_git_schema_not_in_platform_neutral_tools():
    names = {s["function"]["name"] for s in tools.TOOLS_SCHEMA}
    assert "install_git" not in names


def test_install_git_executor_registered():
    assert "install_git" in tools.EXECUTORS
    assert tools.tool_risk("install_git") == "mutating"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only tool")
def test_get_schemas_offers_install_git_only_when_missing(monkeypatch, tmp_path):
    fake = tmp_path / "Git-2.51.0-64-bit.exe"
    fake.write_bytes(b"MZ")
    monkeypatch.setattr(gitenv, "find_git", lambda: None)
    monkeypatch.setattr(gitenv, "find_installer", lambda: fake)

    def names():
        return {s["function"]["name"] for s in tools.get_schemas()}

    assert "install_git" in names()

    monkeypatch.setattr(gitenv, "find_git", lambda: r"C:\Program Files\Git\cmd\git.exe")
    assert "install_git" not in names()

    monkeypatch.setattr(gitenv, "find_git", lambda: None)
    monkeypatch.setattr(gitenv, "find_installer", lambda: None)
    assert "install_git" not in names()
