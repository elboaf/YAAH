"""Windows Git Bash discovery, execution, and runtime guidance tests."""

from pathlib import Path

import pytest

from backend.agent import shell, tools


def _git_install(root: Path) -> Path:
    bash = root / "usr" / "bin" / "bash.exe"
    (root / "cmd" / "git.exe").parent.mkdir(parents=True)
    bash.parent.mkdir(parents=True)
    (root / "cmd" / "git.exe").write_bytes(b"git")
    (root / "bin" / "bash.exe").parent.mkdir(parents=True)
    (root / "bin" / "bash.exe").write_bytes(b"wrapper")
    bash.write_bytes(b"bash")
    return bash


def test_find_git_bash_in_standard_install_and_path(tmp_path, monkeypatch):
    bash = _git_install(tmp_path / "Git")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    wrapper = str((tmp_path / "Git" / "bin" / "bash.exe").resolve())
    assert shell.find_git_bash("") == wrapper
    assert shell.find_git_bash(str(bash.parent)) == wrapper


def test_find_git_bash_skips_unrelated_bash_and_finds_git_layout(tmp_path, monkeypatch):
    _git_install(tmp_path / "Git")
    other = tmp_path / "other"
    other.mkdir()
    (other / "bash.exe").write_bytes(b"not Git Bash")
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert shell.find_git_bash(f"{other};{tmp_path / 'Git' / 'cmd'}") == str((tmp_path / "Git" / "bin" / "bash.exe").resolve())


@pytest.mark.asyncio
async def test_windows_bash_process_uses_git_wrapper(monkeypatch, tmp_path):
    launched = []

    async def fake_exec(*args, **kwargs):
        launched.append((args, kwargs))
        return "process"

    async def forbidden_shell(*args, **kwargs):
        pytest.fail("system shell should not parse Git Bash command")

    monkeypatch.setattr(tools, "resolve_git_bash", lambda env=None: r"C:\Git\bin\bash.exe")
    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tools.asyncio, "create_subprocess_shell", forbidden_shell)
    result = await tools._create_bash_process("echo hi; pwd", tmp_path, {"PATH": ""}, windows=True)
    assert result == "process"
    assert launched[0][0][:3] == (r"C:\Git\bin\bash.exe", "-c", "echo hi; pwd")


@pytest.mark.asyncio
async def test_windows_bash_falls_back_only_when_wrapper_cannot_start(monkeypatch, tmp_path):
    launched = []

    async def failed_exec(*args, **kwargs):
        raise FileNotFoundError("wrapper disappeared")

    async def fake_shell(*args, **kwargs):
        launched.append(args)
        return "system-process"

    monkeypatch.setattr(tools, "resolve_git_bash", lambda env=None: r"C:\Git\bin\bash.exe")
    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", failed_exec)
    monkeypatch.setattr(tools.asyncio, "create_subprocess_shell", fake_shell)
    result = await tools._create_bash_process("echo hi", tmp_path, {}, windows=True)
    assert result == "system-process"
    assert launched == [("echo hi",)]


@pytest.mark.asyncio
async def test_posix_still_uses_native_system_shell(monkeypatch, tmp_path):
    launched = []

    async def fake_shell(*args, **kwargs):
        launched.append(args)
        return "native-process"

    monkeypatch.setattr(tools, "resolve_git_bash", lambda env=None: r"C:\Git\bin\bash.exe")
    monkeypatch.setattr(tools.asyncio, "create_subprocess_shell", fake_shell)
    assert await tools._create_bash_process("echo hi", tmp_path, {}, windows=False) == "native-process"
    assert launched == [("echo hi",)]
