import { describe, expect, it } from 'vitest'
import { attachmentText, INLINE_LIMIT_BYTES, type Attachment } from './attachments'

// Golden fixture (#142): mirrored in backend/tests/test_attachment_inline.py.
// The inline strings here are the EXACT legacy concatenated format the
// backend re-inlines into model context — the two must never drift.
const FIXTURE: Array<[Attachment, string]> = [
  [
    { name: 'notes.md', size: 8, content: '# hello\n' },
    '\n\n--- attached file: notes.md ---\n```\n# hello\n\n```',
  ],
  [
    { name: 'big.log', size: 204_800, path: '.yaah-attachments/big.log' },
    '\n\n--- attached file: big.log (205 KB) ---\n' +
      'Saved to .yaah-attachments/big.log in the workspace. ' +
      'Read it with read_file (use offset/limit for large files).',
  ],
]

describe('attachment inline format (golden fixture)', () => {
  it('uses the shared inline limit', () => {
    expect(INLINE_LIMIT_BYTES).toBe(100_000)
  })

  it('produces the byte-exact legacy inline strings', () => {
    for (const [a, expected] of FIXTURE) {
      expect(attachmentText(a)).toBe(expected)
    }
  })
})
