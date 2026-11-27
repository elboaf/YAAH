#!/bin/bash
# Project-scoped guard against dangerous git commands.
#
# Claude Code feeds the tool call's JSON on stdin; we parse
# tool_input.command and block destructive git operations before they run.
#
# The guard matches the git subcommand at its executable position (skipping
# global options) rather than as a substring of the whole command, so a
# read-only command that merely contains the text "git push" (e.g.
# `grep -n 'git push' README.md`) is allowed, while `git push` and
# `git -C . push` are both blocked.

INPUT=$(cat)

if command -v python3 >/dev/null 2>&1; then
  python3 -c '
import json, shlex, sys

raw = sys.stdin.read()
try:
    data = json.loads(raw)
    command = data.get("tool_input", {}).get("command", "")
except Exception:
    command = ""

if not command:
    sys.exit(0)

# Independent commands are separated by these shell operators.
_SEPS = ("&&", "||", ";", "|")

def _split_commands(cmd):
    """Split on top-level shell operators, respecting single/double quotes."""
    out, cur = [], []
    in_s = in_d = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if ch == "\x27" and not in_d:   # single quote
            in_s = not in_s; cur.append(ch); i += 1; continue
        if ch == "\"" and not in_s:    # double quote
            in_d = not in_d; cur.append(ch); i += 1; continue
        if not in_s and not in_d:
            if cmd[i:i+2] in _SEPS or ch in ";\n":
                out.append("".join(cur)); cur = []; i += 1; continue
        cur.append(ch); i += 1
    if "".join(cur).strip():
        out.append("".join(cur))
    return out

def _git_sub(tokens):
    """Return the git subcommand after skipping global options, or None.

    tokens[0] must be the git executable. Global options are skipped:
    -C <dir> (takes an argument), --opt=value (no separate arg),
    --opt <value>, and other short/long options.
    """
    if not tokens or tokens[0] != "git":
        return None
    i, n = 1, len(tokens)
    while i < n:
        t = tokens[i]
        if t == "--":
            return tokens[i + 1] if i + 1 < n else None
        if t.startswith("--"):
            i += 2 if "=" not in t else 1
            continue
        if t == "-C":
            i += 2
            continue
        if t.startswith("-") and len(t) > 1:
            i += 1
            continue
        return t
    return None

def _block(cmd):
    """True if this command is a dangerous git operation."""
    for seg in _split_commands(cmd):
        try:
            tokens = shlex.split(seg)
        except ValueError:
            continue
        if not tokens:
            continue
        sub = _git_sub(tokens)
        if not sub:
            continue
        if sub == "push":
            return True
        if sub == "reset" and "--hard" in tokens:
            return True
        if sub == "clean" and any(t.startswith("-f") for t in tokens):
            return True
        if sub == "branch" and "-D" in tokens:
            return True
        if sub in ("checkout", "restore") and "." in tokens:
            return True
    return False

try:
    if _block(command):
        print("BLOCKED: %r matches a dangerous git pattern." % command, file=sys.stderr)
        sys.exit(2)
except Exception as e:
    # A guard that cannot parse the command must not crash Claude Code; let it
    # through rather than failing the whole tool call.
    sys.exit(0)
sys.exit(0)
' <<<"$INPUT"
else
  # Fallback: no python3. Parse with a simple sed and substring-match the
  # first token after 'git'. This is less precise than the python path but
  # still blocks the common `git push` case.
  COMMAND=$(echo "$INPUT" | sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\(.*\)".*/\1/p')
  case "$COMMAND" in
    *git\ push*|*git\ reset\ --hard*|*git\ clean\ -f*|*git\ branch\ -D*|*git\ checkout\ .*|*git\ restore\ .*)
      echo "BLOCKED: '$COMMAND' matches a dangerous git pattern." >&2
      exit 2
      ;;
  esac
  exit 0
fi
