// Legacy attachment display parsing (#144).
//
// Before #142, attached text files were concatenated into the user message
// string in an exact template (see attachmentText / the golden fixture in
// src/attachmentFixture.ts). Old conversations store that raw text. This
// parser recovers structured {name,size,content|path} records from it at
// RENDER TIME ONLY — nothing is rewritten: stored content, model replay and
// every other consumer are untouched.
//
// Matching rules (spec: docs/specs/text-attachment-chips.md):
// - Exact-and-anchored: every attachment block starts with "\n\n--- attached
//   file: " and the blocks run to the very end of the message; the marker
//   must sit at the start of a line pair, never mid-prose.
// - Hand-typed lookalikes that merely resemble the marker render untouched.
// - Ambiguous blobs (a fence inside the attached content) refuse to match
//   and render raw, silently — never corrupted or hidden.
// - Any failure returns null; parsing never throws and never mutates.

import type { Attachment } from './attachments'

const MARK = '\n\n--- attached file: '
const POINTER_SUFFIX =
  ' in the workspace. Read it with read_file (use offset/limit for large files).'

export interface ParsedLegacy {
  /** The user's own text with the legacy attachment blocks stripped. */
  text: string
  attachments: Attachment[]
}

/** Parse a legacy-concatenated message into user text + attachment records.
 *  Returns null when the content is not an exact, unambiguous legacy match. */
export function parseLegacyAttachments(content: string): ParsedLegacy | null {
  const first = content.indexOf(MARK)
  if (first === -1) return null

  const attachments: Attachment[] = []
  let i = first
  while (i < content.length) {
    if (!content.startsWith(MARK, i)) return null // junk between/end blocks
    i += MARK.length

    const dash = content.indexOf(' ---\n', i)
    if (dash === -1) return null
    const head = content.slice(i, dash)
    if (!head) return null
    i = dash + 5

    const staged = /^(.+) \((\d+) KB\)$/.exec(head)
    if (staged) {
      // Staged form: exact pointer sentence, byte-for-byte.
      if (!content.startsWith('Saved to ', i)) return null
      const end = content.indexOf(POINTER_SUFFIX, i)
      if (end === -1) return null
      const path = content.slice(i + 'Saved to '.length, end)
      if (!path) return null
      attachments.push({ name: staged[1], size: Number(staged[2]) * 1_000, path })
      i = end + POINTER_SUFFIX.length
    } else {
      // Inline form: plain triple-backtick fence around the content.
      if (!content.startsWith('```\n', i)) return null
      i += 4
      const close = content.indexOf('```', i)
      if (close === -1) return null
      const inner = content.slice(i, close)
      if (inner.includes('```')) return null // ambiguous → raw
      // The template is "```\n" + content + "\n```": exactly one newline
      // always precedes the closing fence, so strip it to recover content.
      if (!inner.endsWith('\n')) return null
      attachments.push({
        name: head,
        size: new TextEncoder().encode(inner).length - 1,
        content: inner.slice(0, -1),
      })
      i = close + 3
    }
  }

  return { text: content.slice(0, first), attachments }
}
