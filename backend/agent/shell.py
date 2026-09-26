"""Shell discovery shared by execution and model-facing runtime guidance."""

import os
from pathlib import Path


def _git_root(candidate: Path) -> Path | None:
    """Return a standard Git for Windows root containing its Bash wrapper."""
    path = candidate
    if path.name.casefold() == "bash.exe":
        parent = path.parent
        if parent.name.casefold() == "bin" and parent.parent.name.casefold() == "mingw64":
            path = parent.parent.parent.parent
        elif parent.name.casefold() == "bin" and parent.parent.name.casefold() == "usr":
            path = parent.parent.parent
        elif parent.name.casefold() == "bin":
            path = parent.parent
        else:
            return None
    elif path.name.casefold() in {"cmd", "bin", "mingw64"}:
        path = path.parent
    if (path / "bin" / "bash.exe").is_file() and (path / "usr" / "bin" / "bash.exe").is_file():
        return path
    return None


def find_git_bash(path: str | None = None) -> str | None:
    """Find Git for Windows' ``bin\\bash.exe`` wrapper, not bare MSYS Bash."""
    search_path = os.environ.get("PATH", "") if path is None else path
    separator = ";" if os.name == "nt" or ";" in search_path else os.pathsep
    roots: list[Path] = []
    for entry_text in search_path.split(separator):
        if not entry_text:
            continue
        entry = Path(entry_text.strip('"'))
        root = _git_root(entry / "bash.exe") or _git_root(entry)
        if root is not None:
            roots.append(root)
    for name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(name)
        if base:
            roots.append(Path(base) / "Git")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Programs" / "Git")
    for root in roots:
        wrapper = root / "bin" / "bash.exe"
        try:
            if _git_root(wrapper) is not None and wrapper.is_file():
                return str(wrapper.resolve())
        except (OSError, RuntimeError):
            continue
    return None


def resolve_git_bash(env: dict | None = None) -> str | None:
    """Resolve Git Bash from the current command environment each time."""
    environment = os.environ if env is None else env
    path_key = next((key for key in environment if key.casefold() == "path"), "PATH")
    return find_git_bash(environment.get(path_key, ""))


def windows_bash_kind() -> str:
    return "git-bash" if resolve_git_bash() else "system"


def windows_bash_note(git_bash: bool) -> str:
    if git_bash:
        return "POSIX shell syntax and utilities."
    return "Windows command syntax (Git Bash unavailable; using the system shell)."
