"""Golden fixture for the attachment inline format (#142).

The canonical name/content pairs and their exact expected inline strings,
shared conceptually with the frontend's copy (src/attachmentFixture.ts).
Backend: re-inlining structured attachments in load_history must produce
these strings byte-identically.
"""
import json

from backend.agent.attachments import (
    INLINE_LIMIT_BYTES,
    inline_attachment_text,
    reinline_attachments,
)


# ---- The golden fixture (mirrored in src/attachmentFixture.ts) ----

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


def test_malformed_records_degrade_safely():
    # Not a list / not dicts / missing name: skip instead of failing the run.
    assert reinline_attachments("hi", None) == "hi"
    assert reinline_attachments("hi", ["nope", 42, {"size": 1}]) == "hi"


def test_records_json_roundtrip():
    """The DB stores a JSON string; reinline must accept the parsed shape."""
    records = json.loads(json.dumps([r for r, _ in FIXTURE]))
    assert reinline_attachments("t", records) == "t" + FIXTURE[0][1] + FIXTURE[1][1]
