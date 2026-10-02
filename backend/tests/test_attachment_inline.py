"""Golden fixture for the attachment inline format (#142).

The canonical name/content pairs and their exact expected inline strings,
canonical frontend copy: src/attachmentFixture.ts (single source of truth).
Backend: re-inlining structured attachments in load_history must produce
these strings byte-identically.
"""
import json

from backend.agent.attachments import (
    INLINE_LIMIT_BYTES,
    inline_attachment_text,
    reinline_attachments,
)


# ---- The golden fixture (canonical copy: src/attachmentFixture.ts) ----

FIXTURE = [
    # (attachment record, exact expected inline string)
    (
        {"name": "notes.md", "size": 8, "content": "# hello\n"},
        "\n\n--- attached file: notes.md ---\n```\n# hello\n\n```",
    ),
    (
        {
            "name": "big.log",
            "size": 204_800,
            "path": ".yaah-attachments/big.log",
        },
        "\n\n--- attached file: big.log (205 KB) ---\n"
        "Saved to .yaah-attachments/big.log in the workspace. "
        "Read it with read_file (use offset/limit for large files).",
    ),
]


def test_inline_limit_matches_frontend():
    assert INLINE_LIMIT_BYTES == 100_000


def test_fixture_inline_strings_are_byte_exact():
    for record, expected in FIXTURE:
        assert inline_attachment_text(record) == expected


def test_reinline_appends_to_user_text():
    text, records = "please review", [r for r, _ in FIXTURE]
    out = reinline_attachments(text, records)
    assert out == text + FIXTURE[0][1] + FIXTURE[1][1]


def test_reinline_no_attachments_is_identity():
    assert reinline_attachments("hello", []) == "hello"
    assert reinline_attachments("hello", None) == "hello"


def test_staged_kb_rounds_up_to_at_least_one():
    record = {"name": "tiny.txt", "size": 10, "path": ".yaah-attachments/tiny.txt"}
    out = inline_attachment_text(record)
    assert "(1 KB)" in out


def test_fixture_strings_are_display_parsable_mirror():
    """#144 mirror: the frontend display parser (src/legacyAttachments.ts)
    is pinned to these exact strings. This test re-derives them locally so a
    wording change here breaks both sides loudly (see the spec's Further
    Notes: the parser pins the OLD wording)."""
    for record, expected in FIXTURE:
        assert inline_attachment_text(record) == expected  # producer parity
        # The strings below must stay in lockstep with src/attachmentFixture.ts.
        assert expected.startswith("\n\n--- attached file: ")
        assert expected.endswith("```") or expected.endswith("large files).")


def test_malformed_records_degrade_safely():
    # Not a list / not dicts / missing name: skip instead of failing the run.
    assert reinline_attachments("hi", None) == "hi"
    assert reinline_attachments("hi", ["nope", 42, {"size": 1}]) == "hi"


def test_records_json_roundtrip():
    """The DB stores a JSON string; reinline must accept the parsed shape."""
    records = json.loads(json.dumps([r for r, _ in FIXTURE]))
    assert reinline_attachments("t", records) == "t" + FIXTURE[0][1] + FIXTURE[1][1]


# ---- Issue #183: the inline cap is enforced at re-inline time ----

def _oversize_record():
    content = "x" * (INLINE_LIMIT_BYTES + 1)
    return {"name": "huge.txt", "size": len(content), "content": content}


def test_overlimit_content_is_not_inlined_in_full():
    """A persisted row with >100 KB content must never ride into model
    context in full: the injection degrades it (truncation marker)."""
    record = _oversize_record()
    out = inline_attachment_text(record)
    assert record["content"] not in out
    assert len(out) < INLINE_LIMIT_BYTES + 500
    assert "…[truncated]" in out


def test_overlimit_content_truncates_at_the_limit():
    record = _oversize_record()
    out = inline_attachment_text(record)
    # The visible head of the content survives, at most the limit.
    assert "x" * INLINE_LIMIT_BYTES in out
    assert "x" * (INLINE_LIMIT_BYTES + 1) not in out


def test_reinline_applies_the_cap_too():
    out = reinline_attachments("hi", [_oversize_record()])
    assert "x" * (INLINE_LIMIT_BYTES + 1) not in out
    assert "…[truncated]" in out


def test_at_limit_content_inlines_in_full():
    """Exactly-at-limit rides inline: the comment says 'at or under'."""
    content = "y" * INLINE_LIMIT_BYTES
    out = inline_attachment_text({"name": "edge.txt", "size": len(content), "content": content})
    assert content in out
    assert "…[truncated]" not in out
