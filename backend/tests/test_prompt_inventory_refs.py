"""Issues #196/#239/#300: guard the prompt-surface inventory's line refs.

docs/research/prompt-surface-inventory.md cites `file.py:A–B` (and `A+`,
`A–B:C–D`, `~A–B`, `:A–B` inline forms) for symbols in backend/agent/*.py.
Those refs rotted within days of being written (the title prompt moved,
`run_agent_turn` was renamed, the base prompt grew) — these tests re-derive
the real spans with ast and fail on drift. Two layers:

1. Name-agnostic sweep (`test_inventory_line_refs_point_at_real_code`,
   #196): a cited range must match SOME top-level symbol span. Catches
   refs that drifted onto nothing at all — but is blind to a ref that
   drifted onto a NEIGHBORING symbol (≥50% overlap tolerance passes it).
2. Named-pair check (`test_inventory_named_symbol_refs_land_inside_the_
   named_symbol`, #300): when a backticked symbol name is followed (within
   a short window) by a parenthesized `file.py:A–B` range, that range must
   land inside THAT symbol's span (or overlap it by ≥50%). This is the
   paired-refs idea of #239 generalized from its one hard-coded pair to
   every `name (file.py:A–B)` citation in the doc — the neighboring-symbol
   drift class that the sweep cannot see.

Both layers stay deliberately tolerant: approximate refs (prefixed `~`)
are skipped; edits inside a cited function don't fail the doc; a moved or
renamed-out-of-existence symbol does. The two-number slash/arrow forms
(`run_agent / _run_agent_claimed (loop.py:a / b)`) keep their dedicated
positional test below (#239 return trips).
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INV = REPO / "docs" / "research" / "prompt-surface-inventory.md"

# file stem -> module path inside backend/agent (only refs into agent code
# are checkable; tools.py/computer.py/... all live there).
AGENT_DIR = REPO / "backend" / "agent"

_REF = re.compile(
    r"\b(?P<file>[a-z_]+\.py):(?P<approx>~)?(?P<a>\d+)(?:\u2013(?P<b>\d+))?"
)

# `symbol name` followed by at most 60 non-paren/non-backtick chars and an
# opening paren that starts a `file.py:A–B` citation. The gap must not
# contain another backtick (that would pair a name with the NEXT name's
# ref) or an opening paren (that would reach across citations). The range
# must end at a delimiter: a trailing ` / <number>` marks the two-number
# positional pairing (`run_agent / _run_agent_claimed (loop.py:a / b)`),
# where which name owns which number is positional and
# test_inventory_paired_name_refs... owns it — the delimiter lookahead
# (rather than a bare negative lookahead) keeps `\d+` from backtracking
# into a shorter number to dodge the exclusion.
_NAMED_REF = re.compile(
    r"`(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\(\))?`[^()`\n]{0,60}\("
    r"(?P<file>[a-z_]+\.py):(?P<approx>~)?(?P<a>\d+)(?:\u2013(?P<b>\d+))?"
    r"(?!\s*/\s*\d)(?=[,)\];]|\s|$)"
)

# Any top-level statement kind counts as a "symbol span" for the sweep —
# including the bare imports / augmented assigns / update() calls that
# wire the schema blocks together (a ref to `TOOLS_SCHEMA +=` line 555 or
# the computer-tools import is citing real code, not drift).
_SPAN_KINDS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
               ast.If, ast.Try, ast.Assign, ast.AnnAssign,
               ast.AugAssign, ast.Expr, ast.Import, ast.ImportFrom)


def _top_level_nodes(path: Path):
    return ast.parse(path.read_text(encoding="utf-8")).body


def _symbol_spans(path: Path) -> list[tuple[int, int]]:
    spans = []
    for node in _top_level_nodes(path):
        if isinstance(node, _SPAN_KINDS) and hasattr(node, "end_lineno"):
            spans.append((node.lineno, node.end_lineno))
    return spans


def _named_symbol_spans(path: Path,
                        names: tuple[str, ...]) -> list[tuple[int, int, str]]:
    out = []
    for node in _top_level_nodes(path):
        name = getattr(node, "name", None)
        if name in names and hasattr(node, "end_lineno"):
            out.append((node.lineno, node.end_lineno, name))
    return out


def _named_span_map(path: Path) -> dict[str, tuple[int, int]]:
    """name -> span for every named top-level node (defs + assigns)."""
    out: dict[str, tuple[int, int]] = {}
    for node in _top_level_nodes(path):
        name = getattr(node, "name", None)
        if name and hasattr(node, "end_lineno"):
            out.setdefault(name, (node.lineno, node.end_lineno))
        for t in getattr(node, "targets", []) or (
                [node.target] if isinstance(node, ast.AnnAssign) else []):
            n = getattr(t, "id", None)
            if n and hasattr(node, "end_lineno"):
                out.setdefault(n, (node.lineno, node.end_lineno))
    return out


def _agent_span_maps() -> dict[str, dict[str, tuple[int, int]]]:
    return {p.name: _named_span_map(p) for p in AGENT_DIR.glob("*.py")}


def _overlap(cited: tuple[int, int], real: tuple[int, int]) -> float:
    lo = max(cited[0], real[0])
    hi = min(cited[1], real[1])
    if hi < lo:
        return 0.0
    return (hi - lo + 1) / min(cited[1] - cited[0] + 1,
                               real[1] - real[0] + 1)


def _lands_in_symbol(cited: tuple[int, int], real: tuple[int, int]) -> bool:
    return (_overlap(cited, real) >= 0.5
            or (real[0] <= cited[0] and real[1] >= cited[1]))


def named_ref_violations(text: str,
                         maps: dict[str, dict[str, tuple[int, int]]]
                         ) -> list[str]:
    """Violations of the named-pair contract (see module docstring).

    Strict containment on purpose: the drift that motivated #300 (a name
    cited with a NEIGHBORING block's range, e.g. `SAY_PERSONAS` as
    loop.py:427–446 vs real 431–465) overlaps its own symbol by ~80% — an
    overlap-tolerance rule would bless exactly that rot. A named ref must
    fall inside the named symbol's span; edits inside a cited function
    still pass (any sub-range of the span is fine).
    """
    bad: list[str] = []
    for m in _NAMED_REF.finditer(text):
        if m.group("approx"):
            continue
        path = AGENT_DIR / m.group("file")
        if not path.is_file():
            continue
        span = maps.get(m.group("file"), {}).get(m.group("name"))
        if span is None:
            continue  # not a top-level symbol of that module: sweep's job
        a = int(m.group("a"))
        b = int(m.group("b") or m.group("a"))
        # Attribute a bare def-line number to the whole symbol (the
        # `run_agent → _run_agent_claimed (loop.py:1573)` flow form).
        if m.group("b") is None:
            return_ok = span[0] <= a <= span[1]
        else:
            return_ok = span[0] <= a and b <= span[1]
        if not return_ok:
            bad.append(f"`{m.group('name')}` cited as {m.group('file')}:"
                       f"{a}\u2013{b} (real span {span[0]}\u2013{span[1]})")
    return bad


def test_inventory_file_exists() -> None:
    # AC1: the README the harness docstring promises is committed.
    assert (REPO / "backend" / "prompt_manifests" / "README.md").is_file()
    assert INV.is_file()


def test_inventory_line_refs_point_at_real_code() -> None:
    text = INV.read_text(encoding="utf-8")
    checked = 0
    stale: list[str] = []
    for m in _REF.finditer(text):
        fname, approx, a, b = (m.group("file"), m.group("approx"),
                               int(m.group("a")), m.group("b"))
        if approx or fname == "scheduler.py":  # scheduler prompt refs move often
            continue
        path = AGENT_DIR / fname
        if not path.is_file():
            continue  # non-agent refs (src/, prompts/) are out of scope
        b = int(b) if b else a
        spans = _symbol_spans(path)
        if not any(
            _overlap((a, b), span) >= 0.5
            or (span[0] <= a and span[1] >= b)  # cited inside a symbol
            for span in spans
        ):
            stale.append(f"{fname}:{a}\u2013{b}")
        checked += 1
    assert checked > 40, "inventory refs not found — regex or path rot"
    assert not stale, (
        f"{len(stale)} stale line refs in prompt-surface-inventory.md "
        f"(point at no top-level symbol in the target module): {stale}"
    )


def test_inventory_named_symbol_refs_land_inside_the_named_symbol() -> None:
    # #300 AC3: the #239 paired-refs check generalized. A backticked symbol
    # name directly followed by a parenthesized `file.py:A–B` range pins
    # the range to THAT symbol: a range that merely overlaps a NEIGHBORING
    # symbol's span (the drift class #300 was filed for) must fail here,
    # while the name-agnostic sweep above stays green.
    text = INV.read_text(encoding="utf-8")
    maps = _agent_span_maps()
    paired = len(_NAMED_REF.findall(text))
    assert paired > 10, (
        "no named `symbol (file.py:A–B)` citations found — regex or "
        "inventory format rot (the named-pair check ran against nothing)")
    bad = named_ref_violations(text, maps)
    assert not bad, (
        f"{len(bad)} named-symbol refs point at the wrong span in "
        f"prompt-surface-inventory.md: " + "; ".join(bad)
    )


def test_named_pair_guard_fails_on_neighboring_symbol_drift() -> None:
    # #300 AC4: the exact rot class from the issue — a cited range that
    # overlaps the WRONG symbol's span. Both snippets cite real-ish spans
    # of loop.py so the name-agnostic sweep would PASS them; the named-pair
    # check must flag the drifted one and accept the correct one.
    maps = _agent_span_maps()
    plan = maps["loop.py"]["_plan_mode_note"]
    base = maps["loop.py"]["_default_system_prompt"]
    drifted = (f"`_plan_mode_note` (loop.py:{base[0]}\u2013{base[1]})")  # neighbor
    correct = (f"`_plan_mode_note` (loop.py:{plan[0]}\u2013{plan[1]})")
    assert named_ref_violations(drifted, maps), (
        "guard accepted a ref that landed inside a NEIGHBORING symbol's span"
    )
    assert not named_ref_violations(correct, maps)
    # And the drift the issue actually reported: the spoken-briefing block
    # cited with the OLD pre-#295 coordinates (a neighboring block).
    old_say = "`SAY_PERSONAS` (loop.py:427\u2013446)"
    assert named_ref_violations(old_say, maps)


def test_inventory_paired_name_refs_point_at_named_symbols() -> None:
    # Return trip on PR #239 review: the inventory cites PAIRED line refs
    # after a symbol name, e.g. `run_agent` / `_run_agent_claimed`
    # (loop.py:1269 / 1236). The name-agnostic span check above cannot tell
    # which number belongs to which name, so a swapped pairing passes it.
    # This test pins the order: positional pairing, names[0]↔a and
    # names[1]↔b — each number must fall inside its own name's span, so a
    # swapped pairing (or a stale ref) fails.
    text = INV.read_text(encoding="utf-8")
    spans = {name: (lo, hi) for lo, hi, name in _named_symbol_spans(
        AGENT_DIR / "loop.py", ("run_agent", "_run_agent_claimed"))}
    assert spans, "expected run_agent/_run_agent_claimed in loop.py"
    # The inventory pairs the two names with either "/" or an arrow
    # ("run_agent / _run_agent_claimed" and the assembly-flow
    # "run_agent → _run_agent_claimed"), so accept both separators —
    # otherwise a stale citation in the arrow form passes undetected.
    sep = r"\s*(?:/|→)\s*"
    pair_re = re.compile(
        r"`(?P<names>run_agent|_run_agent_claimed)`" + sep +
        r"`(?P<names2>run_agent|_run_agent_claimed)`\s*"
        r"\(loop\.py:(?P<a>\d+)(?:\+)?\s*/\s*(?P<b>\d+)"
    )
    mismatches: list[str] = []
    paired = 0
    for m in pair_re.finditer(text):
        n1, n2, a, b = (m.group("names"), m.group("names2"),
                        int(m.group("a")), int(m.group("b")))
        if n1 == n2:
            continue
        paired += 1
        # positional pairing: names[0]↔a, names[1]↔b (the original text had
        # these reversed, which is exactly what this test must catch).
        # Second CodeRabbit return trip: a ref that merely lands INSIDE a
        # function body still passes the span check, so `run_agent` cited
        # with `_run_agent_claimed`'s def line slipped through. A paired
        # name/line ref must cite the symbol's def line exactly.
        for num, name in ((a, n1), (b, n2)):
            if num != spans[name][0]:
                mismatches.append(f"{name} cited as loop.py:{num} "
                                  f"(real span {spans[name][0]}\u2013{spans[name][1]})")
    assert paired > 0, (
        "no paired name refs found — regex or inventory format rot "
        "(the pairing check ran against nothing)")
    assert not mismatches, (
        "paired name refs swapped or stale in prompt-surface-inventory.md: "
        + "; ".join(mismatches)
    )


def test_inventory_no_folklore_size_claim() -> None:
    # AC2: the ~24 KB base-prompt folklore figure must stay corrected.
    # #300 re-measurement (baseline 5dc98b9): bare base 5,923 B, full
    # default 21,014 B — see findings doc §3.
    text = INV.read_text(encoding="utf-8")
    assert "24 KB" not in text.replace("24 KB (absolute", "").replace(
        "24 KB, but", "")
    assert "21,014" in text and "5,923" in text  # measured table survives


@pytest.mark.parametrize("manifest", [
    "win-remote-normal.json", "win-remote-compaction.json",
], )
def test_remote_manifests_carry_no_stripped_schemas(manifest: str) -> None:
    # AC4: stripped sandbox/computer schemas are omitted from remote
    # manifests (regenerated without them in the #172/#200 line), so the
    # byte totals add up instead of counting 6-byte name-only rows.
    import json

    p = REPO / "backend" / "prompt_manifests" / manifest
    schemas = json.loads(p.read_text(encoding="utf-8"))["tool_schemas"]
    stripped = {"sandbox_test", "sandbox_run", "sandbox_status",
                "sandbox_stop", "screenshot", "list_windows",
                "read_ui_tree", "focus_window", "mouse_move", "mouse_click",
                "mouse_drag", "mouse_scroll", "type_text", "press_key",
                "wait"}
    names = {s["name"] for s in schemas}
    assert not names & stripped, f"{manifest} lists stripped schemas: {names & stripped}"
    assert all(s["bytes"] > 16 for s in schemas), "name-only schema rows"
