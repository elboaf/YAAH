"""Locate the bundled GitHub CLI and expose it to workspace shell commands."""

import os
import sys
from pathlib import Path


def bundled_gh_bin_dir() -> Path | None:
    """Return the staged ``gh`` directory for source, desktop, or server builds."""
    exe_dir = Path(sys.executable).parent
    cwd = Path.cwd()
    candidates = []

    # PyInstaller one-file server bundles unpack files below _MEIPASS.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.extend([
            Path(meipass) / "gh" / "bin",
            Path(meipass) / "gh",  # PyInstaller one-file layouts
        ])

    # Tauri app resources live under _up_/backend beside the sidecar.
    candidates.extend([
        exe_dir / "_up_" / "backend" / "gh" / "bin",
        exe_dir / "backend" / "gh" / "bin",
        exe_dir / "gh" / "bin",
        cwd / "_up_" / "backend" / "gh" / "bin",
        cwd / "backend" / "gh" / "bin",
        Path(__file__).parent.parent / "gh" / "bin",
    ])

    executable = "gh.exe" if os.name == "nt" else "gh"
    for candidate in candidates:
        if (candidate / executable).is_file():
            return candidate
    return None


def command_env(env: dict | None = None) -> dict:
    """Copy an environment and prepend the bundled CLI directory to PATH."""
    result = dict(os.environ if env is None else env)
    gh_bin = bundled_gh_bin_dir()
    if gh_bin is None:
        return result

    path_key = next((key for key in result if key.casefold() == "path"), "PATH")
    existing = result.get(path_key, "")
    entries = existing.split(os.pathsep) if existing else []
    if not any(entry.casefold() == str(gh_bin).casefold() for entry in entries):
        result[path_key] = str(gh_bin) + (os.pathsep + existing if existing else "")
    return result
