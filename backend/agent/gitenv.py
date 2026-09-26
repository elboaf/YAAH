"""Git environment detection + the bundled Git-for-Windows installer.

The full official installer (GPLv2, redistribution permitted) is
downloaded by scripts/build_sidecar.sh into backend/installers/ and
shipped as a Windows-only Tauri resource (see
src-tauri/tauri.windows.conf.json). install_git runs it silently for
the agent when git is missing — offered, never silent: the tool is
classified mutating, so the access-mode gate prompts in ask mode.
"""

import os
import shutil
import sys
from pathlib import Path

# Pinned by build_sidecar.sh; keep in sync there.
GIT_INSTALLER_VERSION = "2.51.0"

_GIT_INSTALLER_ENV = "YAAH_GIT_INSTALLER"


def find_git() -> str | None:
    """Path to a runnable git, or None when git is missing."""
    return shutil.which("git")


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent  # backend/


def _installer_dirs() -> list[Path]:
    dirs = []
    if os.environ.get(_GIT_INSTALLER_ENV):
        dirs.append(Path(os.environ[_GIT_INSTALLER_ENV]).parent)
    exe = _exe_dir()
    for base in (exe, exe / "_up_", Path.cwd()):
        dirs += [base / "installers", base / "backend" / "installers"]
    dirs.append(Path(__file__).parent.parent / "installers")  # repo checkout
    return dirs


def find_installer() -> Path | None:
    """Locate the bundled Git-for-Windows installer, if it shipped."""
    for d in _installer_dirs():
        if d.is_file():
            return d
        if d.is_dir():
            hits = sorted(d.glob("Git-*-64-bit.exe"))
            if hits:
                return hits[-1]
    return None


async def run_install_git(workspace: str) -> dict:
    """Silently install the bundled Git for Windows. Windows-only;
    refuses when git already exists or no installer shipped."""
    if os.name != "nt":
        return {"error": "install_git is Windows-only."}
    if find_git():
        return {"ok": True, "already_installed": True,
                "git": find_git(), "note": "git is already installed."}
    installer = find_installer()
    if installer is None:
        return {"error": (
            "No bundled Git installer found in this build. Install git "
            "from https://git-scm.com/download/win and restart YAAH.")}
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        str(installer),
        "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=900)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()  # reap the transport, not just the process (#82)
        return {"error": "Git installer timed out after 900s; aborted."}
    if rc != 0:
        return {"error": f"Git installer exited {rc} (setup logs: "
                         f"%TEMP%\\Git for Windows)."}
    # The running backend does not inherit installer PATH updates. Add the
    # installed Git command wrapper to this process environment immediately.
    installed_git = next((
        candidate for candidate in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd" / "git.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "cmd" / "git.exe",
            Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / "Git" / "cmd" / "git.exe",
        ) if candidate.is_file()
    ), None)
    git = find_git() or (str(installed_git) if installed_git else None)
    if installed_git:
        path_key = next((key for key in os.environ if key.casefold() == "path"), "PATH")
        current = os.environ.get(path_key, "")
        directory = str(installed_git.parent)
        separator = ";" if os.name == "nt" else os.pathsep
        entries = current.split(separator)
        if directory.casefold() not in {p.casefold() for p in entries}:
            os.environ[path_key] = directory + (separator + current if current else "")
    return {"ok": rc == 0, "installed": True, "git": git,
            "note": "Installed. Git and Git Bash are available to subsequent tool calls now."}
