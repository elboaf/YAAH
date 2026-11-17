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


def _front_load_path(env: dict, directory: str) -> None:
    """Make ``directory`` the FIRST PATH entry, exactly once (in-place).

    Unlike _prepend_to_path (which only adds when absent), an existing
    entry further down the PATH is removed and re-prepended: a directory
    that also provides the same command name (an older install) earlier
    in the PATH would otherwise win resolution for spawned shells
    (CodeRabbit finding on #754). When the directory is already first the
    rebuilt PATH is byte-identical, so repeated spawns stay stable.
    """
    path_key = _find_path_key(env)
    existing = env.get(path_key, "")
    separator = ";" if os.name == "nt" else os.pathsep
    kept = (
        [
            entry
            for entry in existing.split(separator)
            if entry.casefold() != directory.casefold()
        ]
        if existing
        else []
    )
    env[path_key] = separator.join([directory, *kept]) if kept else directory


def command_env(env: dict | None = None) -> dict:
    """Copy an environment and prepend the bundled CLI directory to PATH."""
    result = dict(os.environ if env is None else env)
    gh_bin = bundled_gh_bin_dir()
    if gh_bin is None:
        return result
    _prepend_to_path(result, str(gh_bin))
    return result


def interceptor_prepend_dir() -> str | None:
    """The Interceptor install dir tool spawns should carry on PATH, or None.

    The t4-path (#634) seam: one call into the manager, so the spawn sites
    never touch Interceptor directly. Gated on the manager's install state;
    None (Interceptor absent) means callers change nothing. Kept in ghenv
    rather than tools.py so tests can patch the seam at the same module the
    PATH plumbing under test lives in.
    """
    from backend.agent import interceptor

    return interceptor.path_prepend_dir()

