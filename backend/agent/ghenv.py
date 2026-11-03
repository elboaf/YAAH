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


def _find_path_key(env: dict) -> str:
    """Case-insensitive lookup for the PATH key (Windows: 'Path' vs 'PATH')."""
    return next((key for key in env if key.casefold() == "path"), "PATH")


def _prepend_to_path(env: dict, directory: str) -> None:
    """Dedupe-prepend a directory to the environment's PATH (in-place)."""
    path_key = _find_path_key(env)
    existing = env.get(path_key, "")
    separator = ";" if os.name == "nt" else os.pathsep
    entries = existing.split(separator) if existing else []
    if not any(entry.casefold() == directory.casefold() for entry in entries):
        env[path_key] = directory + (separator + existing if existing else "")


def command_env(env: dict | None = None) -> dict:
    """Copy an environment and prepend the bundled CLI directory to PATH."""
    result = dict(os.environ if env is None else env)
    gh_bin = bundled_gh_bin_dir()
    if gh_bin is None:
        return result
    _prepend_to_path(result, str(gh_bin))
    return result
