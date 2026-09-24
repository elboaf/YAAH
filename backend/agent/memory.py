"""Persistent per-project memory: markdown files under ~/.yaah/memory/.

ZCode-style: one fact per file (a slug like ``user-prefers-dark-ui.md``
with frontmatter: ``name``, ``description``, ``metadata.type``) plus a
``MEMORY.md`` index whose one-line entries are injected into the system
prompt every turn. The model decides what is worth remembering (user
preferences, feedback/corrections, project constraints, resource
pointers) and maintains both the files and the index itself via the
memory_save / memory_read / memory_delete tools.

Memory is scoped per workspace — a stable hash of the workspace path
(remote-namespaced workspaces hash their raw ``remote:<host>:<path>``
string, so a remote project's memories stay client-local). The tools
take a slug, never a path: this module resolves
``<memory_root>/<project>/<slug>.md`` itself, so nothing here can touch
files outside the memory root and the sandbox rules are untouched.
"""
import hashlib
import os
import re
from pathlib import Path

# YAAH_MEMORY_PATH lets the test suite redirect this (default is the
# user's real ~/.yaah/memory).
def memory_root() -> Path:
    return Path(
        os.environ.get("YAAH_MEMORY_PATH") or Path.home() / ".yaah" / "memory"
    )


MAX_MEMORY_BODY_CHARS = 20_000
MAX_INDEX_CHARS = 12_000
MAX_SLUG_LEN = 80

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,%d}$" % (MAX_SLUG_LEN - 1))

MEMORY_TEMPLATE = """# Persistent memory

One line per memory file below; the files hold the detail.
"""


def _case_insensitive_fs() -> bool:
    # Module-level so tests can flip it — patching os.name globally breaks
    # pathlib on the CI runner (Path() would try to build a WindowsPath).
    return os.name == "nt"


def project_key(workspace: str | None) -> str:
    """Stable 16-hex key for a workspace. Remote-namespaced workspaces
    (`remote:<host_id>:<path>`) hash the raw namespaced string — those
    paths name files on the host and must never collide with a local
    directory of the same spelling. Everything else hashes the resolved
    workspace root (lower-cased on case-insensitive filesystems so
    `C:\\Proj` and `c:\\proj` share one memory)."""
    from backend.agent.tools import workspace_root

    ws = (workspace or "").strip()
    if ws.startswith("remote:"):
        key_input = ws
    else:
        p = workspace_root(ws)
        key_input = p.as_posix()
        if _case_insensitive_fs():
            key_input = key_input.lower()
    return hashlib.sha1(key_input.encode("utf-8")).hexdigest()[:16]


def memory_dir(workspace: str | None) -> Path:
    return memory_root() / project_key(workspace)


def _slug(name: str) -> str | None:
    slug = (name or "").strip().lower().replace("_", "-")
    slug = re.sub(r"\s+", "-", slug)
    if not _SLUG_RE.match(slug):
        return None
    return slug


def _index_path(workspace: str | None) -> Path:
    return memory_dir(workspace) / "MEMORY.md"


def ensure_dir(workspace: str | None) -> Path:
    """Create the project's memory dir and seed MEMORY.md when missing.
    Never overwrites an existing index. Best-effort; returns the dir."""
    d = memory_dir(workspace)
    if not d.is_dir():
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # callers surface the write error; never crash here
    idx = d / "MEMORY.md"
    if not idx.exists():
        try:
            idx.write_text(MEMORY_TEMPLATE, encoding="utf-8")
        except OSError:
            pass
    return d


def _frontmatter_escape(value: str) -> str:
    import json

    return json.dumps(str(value), ensure_ascii=False)


def index_entry_line(title: str, slug: str, description: str) -> str:
    desc = " ".join(str(description).split())[:200]
    return f"- [{(title or slug).strip()}]({slug}.md) — {desc}"


def _update_index(workspace: str | None, slug: str, entry: str | None) -> None:
    """Rewrite MEMORY.md with `entry` replacing any existing line for
    `slug` (entry=None removes the line). Missing index is recreated."""
    idx = _index_path(workspace)
    lines: list[str] = []
    if idx.exists():
        try:
            lines = idx.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
    marker = f"]({slug}.md)"
    lines = [ln for ln in lines if marker not in ln]
    if entry is not None:
        lines.append(entry)
    try:
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    except OSError:
        pass  # the memory file itself is saved; index is best-effort


def save_memory(
    workspace: str | None,
    name: str,
    title: str,
    description: str,
    mtype: str,
    content: str,
) -> dict:
    """Write one memory file (frontmatter + body) and update the index."""
    slug = _slug(name)
    if slug is None:
        return {
            "error": f"Invalid memory name: {name!r}. Use kebab-case "
            f"(letters, digits, hyphens; max {MAX_SLUG_LEN} chars)."
        }
    if not (content or "").strip():
        return {"error": "content is required (the fact itself)."}
    mtype = str(mtype or "project").strip().lower()
    if mtype not in ("user", "feedback", "project", "reference"):
        mtype = "project"
    d = ensure_dir(workspace)
    path = d / f"{slug}.md"
    text = (
        "---\n"
        f"name: {slug}\n"
        f"description: {_frontmatter_escape(description or title or slug)}\n"
        "metadata:\n"
        f"  type: {mtype}\n"
        "---\n\n"
        f"# {title or slug}\n\n"
        f"{content.strip()}\n"
    )
    try:
        path.write_text(text[: MAX_MEMORY_BODY_CHARS + 2048], encoding="utf-8")
    except OSError as e:
        return {"error": f"Could not write memory: {e}"}
    _update_index(workspace, slug, index_entry_line(title, slug, description))
    return {"saved": slug, "path": str(path), "type": mtype}


def read_memory(workspace: str | None, name: str) -> dict:
    slug = _slug(name)
    if slug is None:
        return {"error": f"Invalid memory name: {name!r}"}
    path = memory_dir(workspace) / f"{slug}.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"error": f"No memory named {slug}"}
    return {"name": slug, "path": str(path), "content": text[:MAX_MEMORY_BODY_CHARS]}


def delete_memory(workspace: str | None, name: str) -> dict:
    slug = _slug(name)
    if slug is None:
        return {"error": f"Invalid memory name: {name!r}"}
    path = memory_dir(workspace) / f"{slug}.md"
    try:
        path.unlink()
    except FileNotFoundError:
        return {"error": f"No memory named {slug}"}
    except OSError as e:
        return {"error": f"Could not delete memory: {e}"}
    _update_index(workspace, slug, None)
    return {"deleted": slug}


# ------------------------------------------------------------ prompt side

_WHEN_TO_SAVE = """\
## When to save a memory
Save proactively (don't wait to be asked) when you learn:
- user: who the user is — role, expertise, stated preferences.
- feedback: guidance they give on how you should work — corrections and \
confirmed approaches. Include **Why:** and **How to apply:** lines.
- project: ongoing work, goals, or constraints NOT derivable from the \
code or git history. Convert relative dates ("next week") to absolute.
- reference: pointers to external resources (URLs, dashboards, tickets).

Do NOT save: what the repo already records (code structure, CLAUDE.md/\
AGENTS.md content, past fixes, git history) or details that only matter \
to the current conversation. Before saving, reuse/update an existing \
memory rather than creating a near-duplicate; delete memories that turn \
out to be wrong. Keep index entries to one line (under ~200 chars); \
the detail goes in the memory file.
"""


def index_for_prompt(workspace: str | None) -> str:
    """The persistent-memory block for the system prompt: the project's
    MEMORY.md index plus save/read/delete usage. Empty string while the
    project has no index yet beyond the template — a fresh project gets
    no prompt noise until a first memory exists. Never raises."""
    idx = _index_path(workspace)
    try:
        text = idx.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001 — optional context must never break a turn
        return ""
    if not text or text == MEMORY_TEMPLATE.strip():
        return ""
    if len(text) > MAX_INDEX_CHARS:
        text = text[:MAX_INDEX_CHARS] + "\n…[truncated]"
    return (
        "# Persistent memory (yours — for THIS project)\n\n"
        "Durable facts you have saved about this user and project; each "
        "line links a file you can read with the memory_read tool:\n\n"
        f"{text}\n\n"
        "Maintain this memory with the memory_save, memory_read and "
        "memory_delete tools.\n\n"
        f"{_WHEN_TO_SAVE}"
    )
