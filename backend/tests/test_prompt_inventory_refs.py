"""Issue #196: guard the prompt-surface inventory's line references.

docs/research/prompt-surface-inventory.md cites `file.py:A–B` (and `A+`,
`A–B:C–D`, `~A–B`, `:A–B` inline forms) for symbols in backend/agent/*.py.
Those refs rotted within days of being written (the title prompt moved,
`run_agent_turn` was renamed, the base prompt grew) — this test re-derives
every top-level symbol's real span with ast and fails on drift. It is
deliberately tolerant: approximate refs (prefixed `~`) are skipped, and a
cited range passes if it CONTAINS the symbol's real span or overlaps it by
>= 50% (edits inside a cited function don't fail the doc; a moved or
renamed-out-of-existence symbol does).
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

# Symbols the inventory cites by name near the ref; we don't parse the prose
# for the symbol name (too fragile) — instead we check each cited range
# against EVERY top-level symbol in the target module: a range that matches
# no symbol span at all is stale. That keeps the test name-agnostic and
# catches renames (`run_agent_turn` -> nothing matched it after the rename).
def _symbol_spans(path: Path) -> list[tuple[int, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    spans = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.If, ast.Try)):
            spans.append((node.lineno, node.end_lineno))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            spans.append((node.lineno, node.end_lineno))
    return spans


def _named_symbol_spans(path: Path,
                        names: tuple[str, ...]) -> list[tuple[int, int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in tree.body:
        name = getattr(node, "name", None)
        if name in names and hasattr(node, "end_lineno"):
            out.append((node.lineno, node.end_lineno, name))
    return out


def _overlap(cited: tuple[int, int], real: tuple[int, int]) -> float:
    lo = max(cited[0], real[0])
    hi = min(cited[1], real[1])
    if hi < lo:
        return 0.0
    return (hi - lo + 1) / min(cited[1] - cited[0] + 1,
                               real[1] - real[0] + 1)


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
    pair_re = re.compile(
        r"`(?P<names>run_agent|_run_agent_claimed)`\s*/\s*"
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
        # these reversed, which is exactly what this test must catch)
        for num, name in ((a, n1), (b, n2)):
            if num not in range(spans[name][0], spans[name][1] + 1):
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
    text = INV.read_text(encoding="utf-8")
    assert "24 KB" not in text.replace("24 KB (absolute", "")
    assert "17,785" in text and "5,176" in text  # measured table survives


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
