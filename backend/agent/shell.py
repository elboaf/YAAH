"""Shell discovery shared by the bash executor and its prompt description."""

import os
from pathlib import Path


def _git_root_for_bash(candidate: Path) -> Path | None:
    """Return the Git for Windows root for a standard Git Bash executable."""
    parts = candidate.parts
    if candidate.name.casefold() != "bash.exe":
        return None
    parent = candidate.parent
    if parent.name.casefold() == "bin" and parent.parent.name.casefold() == "usr":
        root = parent.parent.parent
    elif parent.name.casefold() == "bin":
        root = parent.parent
    else:
        return None

    # Check the installation layout rather than trusting any unrelated
    # bash.exe found earlier on PATH (for example, a WSL launcher).
    if (root / "cmd" / "git.exe").is_file() and (
        root / "usr" / "bin" / "bash.exe"
    ).is_file():
        return root
    return None


def find_git_bash(path: str | None = None) -> str | None:
    """Find Git for Windows' Bash executable, or return None.

    Search PATH entries for the standard Git for Windows layout. This avoids
    mistaking an unrelated ``bash.exe`` (such as a WSL launcher) for Git Bash,
    and still finds Git Bash when another executable named ``bash`` precedes
    it on PATH.
    """
    search_path = os.environ.get("PATH", "") if path is None else path
    # PATH separators may come from a Windows host's environment even when
    # this helper is being unit-tested on POSIX.
    separator = ";" if os.name == "nt" or ";" in search_path else os.pathsep
    entries = [Path(e.strip('"')) for e in search_path.split(separator) if e]
    candidates: list[Path] = []
    roots: list[Path] = []

    for entry in entries:
        candidates.append(entry / "bash.exe")
        # Git for Windows commonly adds only <Git>\\cmd to PATH, not
        # <Git>\\usr\\bin. Derive that installation root as a fallback.
        if (
            entry.name.casefold() == "bin"
            and entry.parent.name.casefold() == "mingw64"
        ):
            roots.append(entry.parent.parent)
        elif entry.name.casefold() in {"cmd", "bin"}:
            roots.append(entry.parent)

    for root in roots:
        candidates.append(root / "usr" / "bin" / "bash.exe")

    # Default per-user and system install locations cover hosts where Git is
    # installed but its command directory was not added to PATH.
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Git" / "usr" / "bin" / "bash.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "Git" / "usr" / "bin" / "bash.exe")

    for candidate in candidates:
        try:
            if candidate.is_file() and _git_root_for_bash(candidate):
                return str(candidate.resolve())
        except (OSError, RuntimeError):
            # Broken PATH entries or inaccessible installation paths should
            # behave exactly like Git Bash not being installed.
            continue
    return None


def windows_bash_kind() -> str:
    """Shell the Windows bash tool will select before process startup."""
    return "git-bash" if find_git_bash() else "cmd"


def windows_bash_note(git_bash: bool) -> str:
    """Prompt/help text explaining the shell actually selected on Windows."""
    if git_bash:
        return "POSIX shell syntax and utilities."
    return "Windows command syntax (Git Bash unavailable; using the system shell)"
