"""Bundled GitHub CLI resource discovery and shell PATH integration."""

import os
from backend.agent import ghenv


def test_bundled_gh_bin_dir_finds_tauri_resource(monkeypatch, tmp_path):
    exe_dir = tmp_path / "app"
    gh_bin = exe_dir / "_up_" / "backend" / "gh" / "bin"
    gh_bin.mkdir(parents=True)
    executable = "gh.exe" if os.name == "nt" else "gh"
    (gh_bin / executable).write_bytes(b"fake")

    monkeypatch.setattr(ghenv.sys, "executable", str(exe_dir / "yaah-backend.exe"))
    monkeypatch.setattr(ghenv.sys, "_MEIPASS", None, raising=False)
    monkeypatch.setattr(ghenv.os, "name", os.name)

    assert ghenv.bundled_gh_bin_dir() == gh_bin


def test_bundled_gh_bin_dir_finds_pyinstaller_resource(monkeypatch, tmp_path):
    meipass = tmp_path / "_MEI123"
    gh_bin = meipass / "gh" / "bin"
    gh_bin.mkdir(parents=True)
    executable = "gh.exe" if os.name == "nt" else "gh"
    (gh_bin / executable).write_bytes(b"fake")

    monkeypatch.setattr(ghenv.sys, "executable", str(tmp_path / "server.exe"))
    monkeypatch.setattr(ghenv.sys, "_MEIPASS", str(meipass), raising=False)

    assert ghenv.bundled_gh_bin_dir() == gh_bin


def test_command_env_prepends_bundled_cli_without_mutating_input(monkeypatch, tmp_path):
    gh_bin = tmp_path / "gh" / "bin"
    gh_bin.mkdir(parents=True)
    executable = "gh.exe" if os.name == "nt" else "gh"
    (gh_bin / executable).write_bytes(b"fake")
    monkeypatch.setattr(ghenv, "bundled_gh_bin_dir", lambda: gh_bin)

    original = {"PATH": f"existing{os.pathsep}path", "OTHER": "kept"}
    actual = ghenv.command_env(original)

    assert actual["PATH"].split(os.pathsep) == [str(gh_bin), "existing", "path"]
    assert actual["OTHER"] == "kept"
    assert original["PATH"] == f"existing{os.pathsep}path"


def test_command_env_does_not_duplicate_bundled_cli(monkeypatch, tmp_path):
    gh_bin = tmp_path / "gh" / "bin"
    gh_bin.mkdir(parents=True)
    monkeypatch.setattr(ghenv, "bundled_gh_bin_dir", lambda: gh_bin)
    env = {"PATH": f"{gh_bin}{os.pathsep}other"}

    assert ghenv.command_env(env)["PATH"] == env["PATH"]


def test_command_env_falls_back_when_cli_is_not_staged(monkeypatch):
    monkeypatch.setattr(ghenv, "bundled_gh_bin_dir", lambda: None)
    env = {"PATH": "existing"}

    assert ghenv.command_env(env) == env
