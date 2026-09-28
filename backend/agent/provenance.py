"""Write provenance (issue #98 / docs/adr/0002): what did this turn WRITE?

Extracted from worktrees.py (ADR 0007 ticket 2: tenant split). This is
the pure registry + classification tenant — no git, no lifecycle. The
trash classifier can only refuse-or-drop a file when it can tell WORK
from TRASH. Tools report what they wrote here as they write it; the
harness seeds the registry before a write tool call and clears it when
the worktree is torn down. Two classes:
  model — content the model authored (write_file/create_file/edit_file
          payloads): real work, treated as precious.
  tool  — harness-generated captures (bash/powershell output redirect
          captures written by the harness): trash unless the model then
          edited the same path itself.
Provenance is a hint, never a veto: an unknown path (say, an `npm install`
that wrote package-lock.json) falls through to the shape heuristics, and
the user's own files in the main tree are untouched by all of this.
"""

from __future__ import annotations

import re
from pathlib import Path

_WRITE_PROVENANCE: dict[str, dict[str, set[str]]] = {}
# workspace -> {"model": {relpath, ...}, "tool": {relpath, ...}}

# Windows/POSIX path normalization for registry keys: forward slashes,
# lowercased drive, so `C:\x\y` and `c:/x/y` are one entry.
def _norm_path(p: str | Path) -> str:
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        s = s[0].upper() + s[1:]
    return s


def note_write(workspace: str, path: str, kind: str) -> None:
    """Record that a tool wrote `path` in `workspace` (kind: model|tool)."""
    if kind not in ("model", "tool"):
        return
    ws = _WRITE_PROVENANCE.setdefault(_norm_path(workspace), {})
    ws.setdefault(kind, set()).add(_norm_path(path))


def provenance_for(workspace: str, path: str) -> str | None:
    """'model' | 'tool' when this turn wrote the path, else None.

    Accepts the path relative to `workspace` or absolute; both registered
    forms are checked, and MODEL always wins (a capture the model later
    edited is authored work, not a capture)."""
    ws = _WRITE_PROVENANCE.get(_norm_path(workspace))
    if not ws:
        return None
    np = _norm_path(path)
    cands = {np}
    try:
        cands.add(_norm_path(Path(workspace) / path))
    except OSError:
        pass
    if any(c in ws.get("model", ()) for c in cands):
        return "model"
    if any(c in ws.get("tool", ()) for c in cands):
        return "tool"
    return None


def clear_provenance(workspace: str) -> None:
    _WRITE_PROVENANCE.pop(_norm_path(workspace), None)


# Shell output redirections: `cmd > f`, `cmd >> f`, `cmd 2> f`, `cmd &> f`,
# `cmd 2>&1 > f`, and the PowerShell twins `Out-File f` / `> f`. The harness
# itself writes these capture files when a command's stdout is redirected
# into the workspace — content the model never authored — so they register
# as `tool` provenance and the classifier can drop them at merge time.
_REDIRECT_RE = re.compile(
    r"(?:^|[\s;&|(])(?:\d?\s*>+|\d?&>|&>)\s*([^\s|&;<>]+)"
    r"|(?:^|[\s;&|(])out-file\s+(?:-\w+\s+)*([^\s|&;<>-]+)",
    re.IGNORECASE,
)


def note_shell_writes(workspace: str, command: str) -> None:
    """Register redirection targets in `command` as tool-written paths.
    Best-effort by contract: parsing is heuristic (quotes, subshells and
    expansions are not interpreted); a miss just means the file falls back
    to the shape heuristics."""
    for m in _REDIRECT_RE.finditer(command or ""):
        target = m.group(1) or m.group(2)
        if target:
            note_write(workspace, target, "tool")


def provenance_classify(workspace: str, wt: Path, paths: list[str]) -> dict[str, str]:
    """provenance class per dirty path: model > tool > unknown."""
    out: dict[str, str] = {}
    for rel in paths:
        cls = provenance_for(wt, rel) or provenance_for(wt, wt / rel)
        out[rel] = cls or "unknown"
    return out


# Machine-shape fingerprints (adr/0002): content that only a build tool,
# test runner, or redirect produces. Conservative by design — a miss just
# means the file takes the precious path (salvage + refusal), exactly the
# pre-#98 behavior; a false TRASH is the only dangerous direction, so the
# checks are structural, not name-based wishful thinking.
_TRASH_EXTS = {
    ".log", ".tmp", ".temp", ".swp", ".swo", ".pyc", ".pyo",
    ".patch", ".rej", ".orig", ".bak", ".salvage", ".out",
    # .txt (adr/0002): in a WORKTREE, uncommitted .txt is overwhelmingly a
    # command capture or tool dump — authored text goes through
    # write_file/create_file (provenance `model`, protected regardless).
    # This is the one knowingly-imperfect extension: an agent-authored
    # .txt that arrived via a parse-missed redirect would be dropped
    # (residual risk accepted; the content also lives in the transcript).
    ".txt",
}

_TRASH_NAMES = {"npm-debug.log", "yarn-error.log", "yarn.lock.check", ".DS_Store"}

# Extensions a redirect capture plausibly uses (adr/0002): tool-provenance
# files with these extensions are captures, not authored documents. Authored
# extensions (.md, code files) keep the precious path even when the model
# created them via a redirect — `gh pr view 96 > notes.md` is intent.
_CAPTURE_EXTS = {".txt", ".json", ".csv", ".tsv", ".ndjson", ".xml", ".yaml", ".yml"}


def is_machine_shape(p: Path) -> bool:
    """Structural fingerprints of generated output. All byte sniffing is
    capped (32 KB head / 4 KB tail) — this runs per dirty file at merge
    time and must stay cheap."""
    try:
        if not p.is_file():
            return False
        with p.open("rb") as f:
            head = f.read(32768)
            if p.stat().st_size > 4096:
                f.seek(-4096, 2)  # tail window, relative to EOF
            tail = f.read(4096)
    except OSError:
        return False
    if not head:
        return False
    # diff/patch walls: git salvage patches, compiler error dumps
    stripped = head.lstrip()
    if any(
        stripped.startswith(sig)
        for sig in (b"diff ", b"--- ", b"+++ ", b"@@ -", b"Index:")
    ):
        return True
    # unified-diff body (our salvage patches carry a comment header first)
    if b"\ndiff --git " in head and b"\n+++" in head:
        return True
    # base64 wall: saved crash dumps / image captures dropped as text
    dense = sum(1 for ch in head if 48 <= ch <= 122)
    if len(head) >= 1024 and dense / len(head) > 0.97:
        return True
    # JSON object/array with a parsed balanced-bracket budget: build
    # manifests, test-output envelopes, tsbuildinfo — but NOT a hand-written
    # config the model may have authored (those are short; the size gate
    # plus the depth requirement keeps them out of TRASH).
    body = head.strip()
    if body[:1] in (b"{", b"[") and len(body) > 256:
        depth = curly = 0
        in_str = False
        esc = False
        for ch in body:
            byte = ch.to_bytes(1, "big")
            if esc:
                esc = False
            elif byte == b"\\":
                esc = True
            elif byte == b'"':
                in_str = not in_str
            elif not in_str:
                if byte == b"{":
                    curly += 1
                    depth = max(depth, curly)
                elif byte == b"}":
                    curly -= 1
        if depth >= 2 and curly == 0:
            return True
    # log-ish tail: line after line of timestamps/levels/severities
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if len(lines) >= 4:
        logish = sum(
            1
            for ln in lines
            if ln[:1].isdigit()
            or ln[:1] == b"["
            or b"ERROR" in ln
            or b"DEBUG" in ln
            or b"WARNING" in ln
        )
        if logish / len(lines) >= 0.75:
            return True
    return False


def trash_class(worktree_dirty: list[str], wt: Path) -> dict[str, str]:
    """Classify each dirty path: 'work' (precious) or 'trash' (droppable).

    A path is TRASH only when provenance says harness-generated, or when it
    carries a machine-shape fingerprint (extension, name, or content shape).
    Everything else — anything authored-looking, anything uncertain — is
    WORK, which takes the old refuse+salvage path. (adr/0002: only a false
    'trash' can lose work, so uncertainty always lands on WORK.)
    """
    classes = provenance_classify(wt, wt, worktree_dirty)
    out: dict[str, str] = {}
    for rel, cls in classes.items():
        if cls == "model":
            out[rel] = "work"
            continue
        p = wt / rel
        ext = p.suffix.lower()
        if cls == "tool":
            # The harness captured it — trash when the extension says
            # capture (a redirected .md / code file stays precious: the
            # model aimed output at a real artifact).
            if ext in _CAPTURE_EXTS or ext in _TRASH_EXTS:
                out[rel] = "trash"
                continue
            out[rel] = "work"
            continue
        if ext in _TRASH_EXTS or p.name in _TRASH_NAMES:
            out[rel] = "trash"
            continue
        if is_machine_shape(p):
            out[rel] = "trash"
            continue
        out[rel] = "work"
    return out
