"""Structured text attachments (#142): the inline format and re-inlining.

Attachments travel as data alongside the message ({name, size,
content?|path?}); the visible message content holds only the user's typed
text. The model still sees every byte: load_history re-inlines the
attachment records into the user text in the EXACT legacy concatenated
format, pinned by the golden fixture in test_attachment_inline.py
(canonical frontend copy: src/attachmentFixture.ts).
"""
# Files at or under this ride inline (content present on the record);
# anything bigger is staged in the workspace and referenced by path.
INLINE_LIMIT_BYTES = 100_000
_TRUNCATION_MARKER = "…[truncated]"


def inline_attachment_text(record: dict) -> str:
    """The exact legacy inline string one attachment record contributes.

    The inline cap is enforced HERE, not just in the composer: a record
    that reaches the injection layer with >INLINE_LIMIT_BYTES of content
    (a pre-#142 row, or a hand-rolled API call) is truncated with the
    …[truncated] marker instead of riding into model context in full —
    the wrapper comment's "at or under" rule is true of this layer.
    """
    name = record.get("name") or "attachment"
    content = record.get("content")
    if content is not None:
        if len(content.encode("utf-8")) > INLINE_LIMIT_BYTES:
            cut = content
            while len(cut.encode("utf-8")) > INLINE_LIMIT_BYTES:
                cut = cut[:-1]
            content = cut + "\n" + _TRUNCATION_MARKER
        return f"\n\n--- attached file: {name} ---\n```\n{content}\n```"
    path = record.get("path") or ""
    kb = max(1, int(record.get("size", 0) / 1_000 + 0.5))  # Math.round parity
    return (
        f"\n\n--- attached file: {name} ({kb} KB) ---\n"
        f"Saved to {path} in the workspace. "
        f"Read it with read_file (use offset/limit for large files)."
    )


def reinline_attachments(text: str, records) -> str:
    """Append every attachment's inline text to the user's message text.

    Degrades safely: a malformed record (non-list payload, non-dict entry,
    missing name) is skipped — a display-side storage glitch must never
    fail the turn.
    """
    if not records:
        return text
    out = text
    for record in records:
        if not isinstance(record, dict) or not record.get("name"):
            continue
        out += inline_attachment_text(record)
    return out
