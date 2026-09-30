# Spec: text attachments show as attachment chips in the transcript

## Problem Statement

When a user attaches a text file in a YAAH chat, the entire file content is baked
into the message text as a fenced block. The transcript then renders that blob
verbatim in the user bubble: a 100KB file is a wall of unstyled monospace text,
drowning both the user's own words and the rest of the transcript. The user wants
the transcript to show a compact attachment chip (like images already get), while
the model still receives the full text.

## Solution

Text attachments become structured data for their whole life — composer, wire,
database — and are rendered as compact filename+size chips with a collapsible
inline text expansion in the transcript. The backend re-inlines the attachment
text into model context in today's exact fenced format, so what the model sees
does not change at all. Existing conversations render uniform chips too:
at render time, messages without structured attachments whose content matches
the legacy fenced-block shape (or the legacy "Saved to …" pointer sentence)
display as chips instead of raw text, with ambiguous matches falling back to
today's raw rendering.

## User Stories

1. As a chat user, I want an attached text file to appear as a compact chip in
   my message bubble, so that my own words stay readable next to the
   attachment.
2. As a chat user, I want the chip to show the filename and its size, so that I
   can tell at a glance what was attached without reading its content.
3. As a chat user, I want to click the chip to expand the file's text inline
   below it, so that I can read the attachment without leaving the transcript.
4. As a chat user, I want to click the expanded chip again to collapse it, so
   that the transcript returns to its compact form.
5. As a chat user, I want the expanded text capped with internal scrolling, so
   that a large attachment cannot take over the screen.
6. As a chat user attaching a large file, I want its chip to work the same as a
   small file's, so that behavior is uniform regardless of size.
7. As a chat user attaching a large file, I want the chip's expansion to fetch
   the staged file's content when I open it, so that the transcript stays
   lightweight until I actually read it.
8. As a chat user reading an old conversation, I want my old attached-file
   messages to display as chips too, so that the whole transcript looks
   uniform.
9. As a chat user reading an old conversation, I want old large-file pointer
   lines ("Saved to .yaah-attachments/…") to display as staged chips, so that
   they get the same expand-to-read behavior as new messages.
10. As a chat user with an unusual old message (fences inside the attached
    content), I want it to render exactly as it did before, so that nothing I
    wrote is ever shown differently or lost.
11. As a chat user whose queued message carries attachments, I want the chip to
    show in the queued echo and persist with the queued item, so that steering
    behaves like a normal send.
12. As a chat user whose send fails mid-turn, I want my draft's attachments
    restored along with the text, so that a failed send loses nothing.
13. As a chat user on a remote-host workspace, I want chips to work identically
    there, so that the feature follows YAAH's remote parity.
14. As a chat user, I want the model to keep receiving the full attachment text
    exactly as before, so that agent behavior never changes because of a
    display feature.
15. As a chat user, I want the composer's staging chips to keep working as
    today, so that attaching feels unchanged.
16. As an agent reading the transcript via its own tooling, I want old
    conversations' model context to stay byte-identical, so that history
    replay does not drift.
17. As a developer of YAAH, I want a single canonical fixture of the inline
    format shared by both sides' tests, so that the backend's re-inlining and
    the frontend's legacy parser cannot drift apart.
18. As a developer of YAAH, I want the legacy display parser to match the exact
    legacy marker and fence shape, so that hand-typed lookalike text in normal
    messages renders untouched.
19. As a developer of YAAH, I want ambiguous legacy blobs to fall back to raw
    rendering, so that the parser can never corrupt what the user reads.
20. As a maintainer, I want a database migration that adds the attachments
    column to existing databases, so that upgrades are seamless.
21. As a maintainer, I want the attachments records to keep the staged file's
    path for large files, so that agents can still read_file them and the chip
    can still fetch content later.
22. As a maintainer, I want images' flow untouched, so that the change is
    scoped to text attachments only.

## Implementation Decisions

- **Structured side-channel (the images pattern).** Text attachments travel as
  data alongside the message instead of being concatenated into the visible
  message string. The user-visible message content becomes the user's typed
  text only. Images' payload/DB flow is untouched.
- **Wire shape.** Send and queue endpoints accept an optional
  `attachments` array alongside `images`: one record per attachment
  `{name, size, content?|path?}` — `content` present for files ≤ 100KB
  (INLINE_LIMIT_BYTES), `path` (workspace-relative, under
  `.yaah-attachments/`) for larger staged files. At send time the frontend
  still stages >100KB files via `POST /api/attachments` exactly as today;
  ≤100KB files no longer need staging at all.
- **Persistence.** New `attachments` JSON column on the `messages` table
  (migration follows the images-column pattern), storing one JSON record per
  attachment `{name, size, content?|path?}`. The model-facing content string
  is stored WITHOUT the fenced blocks.
- **Model context.** `load_history` re-inlines structured attachments into the
  user text part, byte-identical to today's format:
  `\n\n--- attached file: <name> ---\n` + triple-backtick fence + content +
  fence for inline files; the exact current pointer sentence for staged files
  (`--- attached file: <name> (<kb> KB) ---\nSaved to <path> in the workspace.
  Read it with read_file (use offset/limit for large files).`). A golden
  fixture (the canonical name/content pairs and their exact expected inline
  strings) pins this format and is shared by backend and frontend tests so the
  two cannot drift. Re-inlining happens at context-build time, never at
  persist time.
- **Frontend render.** The user bubble renders attachment chips in the same
  region as image thumbnails (images row, then chips, then text). Chip =
  filename + size, matching the composer chip style (10px mono on #27272a,
  #d4d4d8 text). Click toggles a collapsible inline expansion below the chip:
  monospace, capped height with internal scrolling, collapse on second click.
  Inline-content chips render from the row's own data; staged-path chips fetch
  content lazily via `POST /api/files/preview` (which already proxies to
  remote hosts); a fetch miss renders a graceful "file no longer exists"
  state.
- **Legacy parse-for-display, no DB rewrite.** The user bubble, when a message
  has no structured attachments but its content matches the EXACT legacy
  shape — leading `--- attached file: X ---` marker line(s) + plain triple-
  backtick fences + trailing pointer sentence for staged files — displays
  chips instead. Matching is exact-and-anchored to the legacy template;
  hand-typed lookalike text mid-prose never becomes chips. Ambiguous matches
  (fences inside the file content) render raw as today, silently. Old
  conversations pick this up on next transcript load. Purely presentational:
  stored content, model replay, and export/other consumers unchanged.
- **Queue/steer parity.** The queue endpoint and queue echo carry structured
  attachments like send; boundary injection persists the attachments with the
  user row, and the re-inliner applies to queued injections exactly as to
  sent messages.
- **Draft safety.** The failed-send draft restore includes the attachments
  array (today's code already captures and restores it; it must keep working
  with the new structured array).
- **Sub-agents, PTT, and handoff paths are out of the diff** except where the
  shared `send` path necessarily flows through: they pass no text attachments
  today and continue to pass none.
- **Limits unchanged**: 2MB max text file, NUL-byte binary rejection, 100KB
  inline threshold — same values, now enforced on the structured record.

## Testing Decisions

- **What makes a good test here**: only external behavior — what the model
  context contains, what the API persists and returns, and what the transcript
  displays. Never assert on component internals or private helpers.
- **Seam 1 — backend model-context replay (highest)**: persist a message with
  structured attachments, assert the model-facing user text is byte-identical
  to the golden format (inline case, staged case, mixed text+attachments,
  plus a golden-fixture comparison against today's concatenated string shape).
  Prior art: image-parts replay tests in test_agent.py.
- **Seam 2 — API**: send and queue endpoints accepting structured attachments
  persist the row correctly (content without fences, attachments JSON intact,
  images untouched); the remote proxy passes the attachments array through
  verbatim. Prior art: test_attachments.py, test_remote.py.
- **Seam 3 — frontend render**: user bubbles render chips from structured
  rows; expand/collapse and lazy fetch with graceful miss; legacy display
  parsing (blob → chips, pointer → staged chip, ambiguous → raw, hand-typed
  lookalike → raw). Prior art: imageViewer.test.tsx.
- **Golden fixture**: one canonical fixture (name/content pairs ↔ exact
  expected inline strings) imported by both the backend replay tests and the
  frontend legacy-parser tests, so re-inlining and parsing share one source of
  truth for the format.
- **Regression guard**: a test pins that a legacy-concatenated message string
  and a structured message with the same attachments produce identical model
  context after re-inlining.

## Out of Scope

- Changing the model-facing format (fenced blocks, pointer sentence) —
  byte-identical replay is a requirement, not an optimization.
- Changing inline/staged limits, binary rejection, or the images flow.
- A DB rewrite or backfill of legacy messages.
- Edit/resend of old messages.
- Markdown rendering of user bubbles.
- Any new attachment types (PDFs, binary files).
- The composer's staging UI (chips, drag-drop, paste, size checks).

## Further Notes

- The legacy pointer sentence for staged files is part of the exact-match
  template; if the sentence ever changes wording, the legacy parser must pin
  the OLD wording for old rows, and new rows stop needing the parser at all.
- Because ≤100KB files were never staged to disk, their transcript data is
  the only copy — inline content storage keeps history immune to later
  workspace edits.
- Sub-agent transcripts are rendered separately and are untouched.
- Backend repro notes for implementers: the last verified layout is
  `src/components.tsx` (Attachment interface ~8006, attachmentText ~8028,
  addFiles ~8765, send ~9165, queueInput ~9371, MessageView user branch
  ~1392); `src/api.ts` (uploadAttachment ~792, queueMessage ~544);
  `backend/main.py` (/api/attachments ~1059, send ~990, queue ~1121,
  /api/files/preview ~1822); `backend/agent/loop.py` (load_history ~1011);
  `backend/db/database.py` (messages schema ~66, migrations ~195). Line
  numbers WILL drift; grep for the symbols.
