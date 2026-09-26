"""Windows Git Bash discovery and bash-tool shell selection tests."""

import os
from pathlib import Path

import pytest

from backend.agent import shell, tools


def _git_install(root: Path) -> Path:
    bash = root / "usr" / "bin" / "bash.exe"
    git = root / "cmd" / "git.exe"
    bash.parent.mkdir(parents=True)
    git.parent.mkdir(parents=True)
    bash.write_bytes(b"MZ bash")
    git.write_bytes(b"MZ git")
    return bash


def test_find_git_bash_from_standard_path_entries(tmp_path):
    bash = _git_install(tmp_path / "Git")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "bash.exe").write_bytes(b"MZ other")

    found = shell.find_git_bash(os.pathsep.join((str(unrelated), str(bash.parent))))
    assert found == str(bash.resolve())


def test_find_git_bash_rejects_non_git_bash(tmp_path, monkeypatch):
    unrelated = tmp_path / "bash.exe"
    unrelated.write_bytes(b"MZ other")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "program-files"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert shell.find_git_bash(str(tmp_path)) is None


def test_find_git_bash_from_git_cmd_path_entry(tmp_path):
    bash = _git_install(tmp_path / "Git")
    cmd = tmp_path / "Git" / "cmd"
    cmd.mkdir(exist_ok=True)

    assert shell.find_git_bash(str(cmd)) == str(bash.resolve())


@pytest.mark.asyncio
async def test_windows_bash_process_uses_git_bash_without_cmd(monkeypatch, tmp_path):
    launched = []

    async def fake_exec(*args, **kwargs):
        launched.append(("exec", args, kwargs))
        return "process"

    async def forbidden_shell(*args, **kwargs):
        pytest.fail("cmd/system shell should not parse the command")

    monkeypatch.setattr(tools, "find_git_bash", lambda path=None: r"C:\Git\usr\bin\bash.exe")
    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tools.asyncio, "create_subprocess_shell", forbidden_shell)

    result = await tools._create_bash_process("echo hi; pwd", tmp_path, {"PATH": ""}, windows=True)
    assert result == "process"
    assert launched[0][1][:3] == (r"C:\Git\usr\bin\bash.exe", "-c", "echo hi; pwd")


@pytest.mark.asyncio
async def test_windows_bash_process_falls_back_if_git_bash_wont_start(monkeypatch, tmp_path):
    launched = []

    async def failed_exec(*args, **kwargs):
        raise FileNotFoundError("bash disappeared")

    async def fake_shell(*args, **kwargs):
        launched.append((args, kwargs))
        return "cmd-process"

    monkeypatch.setattr(tools, "find_git_bash", lambda path=None: r"C:\Git\usr\bin\bash.exe")
    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", failed_exec)
    monkeypatch.setattr(tools.asyncio, "create_subprocess_shell", fake_shell)

    result = await tools._create_bash_process("echo hi", tmp_path, {"PATH": ""}, windows=True)
    assert result == "cmd-process"
    assert launched[0][0] == ("echo hi",)


@pytest.mark.asyncio
async def test_posix_bash_process_keeps_native_shell_even_if_git_bash_exists(monkeypatch, tmp_path):
    launched = []

    async def forbidden_exec(*args, **kwargs):
        pytest.fail("POSIX must not launch Git Bash")

    async def fake_shell(*args, **kwargs):
        launched.append((args, kwargs))
        return "posix-process"

    monkeypatch.setattr(tools, "find_git_bash", lambda path=None: r"C:\Git\usr\bin\bash.exe")
    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", forbidden_exec)
    monkeypatch.setattr(tools.asyncio, "create_subprocess_shell", fake_shell)

    result = await tools._create_bash_process("echo hi", tmp_path, {}, windows=False)
    assert result == "posix-process"
    assert launched[0][0] == ("echo hi",)
