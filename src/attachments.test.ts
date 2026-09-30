import { describe, expect, it } from 'vitest'
import { attachmentText, INLINE_LIMIT_BYTES, type Attachment } from './attachments'
import { FIXTURE } from './attachmentFixture'
import { parseLegacyAttachments } from './legacyAttachments'

// Golden fixture (#142): mirrored in backend/tests/test_attachment_inline.py.
// The inline strings here are the EXACT legacy concatenated format the
// backend re-inlines into model context — the two must never drift. The
// fixture itself lives in src/attachmentFixture.ts (shared with the #144
// display parser).
export { FIXTURE }

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

describe('parseLegacyAttachments (#144: legacy display parsing)', () => {
  // The exact strings the backend used to concatenate, regenerated via
  // attachmentText so the parser and producer share the fixture.
  const inlineFull = 'please review' + attachmentText(FIXTURE[0][0])
  const stagedFull = 'check this' + attachmentText(FIXTURE[1][0])

  it('round-trips inline legacy blobs back to structured records', () => {
    const parsed = parseLegacyAttachments(inlineFull)
    expect(parsed).not.toBeNull()
    expect(parsed!.text).toBe('please review')
    expect(parsed!.attachments).toEqual([
      { name: 'notes.md', size: 8, content: '# hello\n' },
    ])
  })

  it('round-trips staged pointer messages back to records', () => {
    const parsed = parseLegacyAttachments(stagedFull)
    expect(parsed).not.toBeNull()
    expect(parsed!.text).toBe('check this')
    // The staged form carries a KB-rounded size; it reconstructs exactly
    // what the chip displays (kb * 1000), not the pre-rounding byte count.
    expect(parsed!.attachments).toEqual([
      { name: 'big.log', size: 205_000, path: '.yaah-attachments/big.log' },
    ])
  })

  it('handles multiple legacy attachments in one message', () => {
    const parsed = parseLegacyAttachments(
      'two files' + attachmentText(FIXTURE[0][0]) + attachmentText(FIXTURE[1][0]),
    )
    expect(parsed).not.toBeNull()
    expect(parsed!.text).toBe('two files')
    expect(parsed!.attachments).toHaveLength(2)
  })

  it('returns null for plain messages without any marker', () => {
    expect(parseLegacyAttachments('just a normal message')).toBeNull()
    expect(parseLegacyAttachments('')).toBeNull()
  })

  it('never matches hand-typed lookalikes mid-prose', () => {
    // Marker not anchored at the start of a trailing segment: prose before
    // it on the same line disqualifies the match.
    const lookalike =
      'I typed this myself\n\nlook --- attached file: fake.txt ---\n```\nhi\n```'
    expect(parseLegacyAttachments(lookalike)).toBeNull()
    // Wrong fence count (unclosed fence) — not the exact template.
    expect(
      parseLegacyAttachments('x\n\n--- attached file: a.txt ---\n```\nbody'),
    ).toBeNull()
    // Wrong pointer wording.
    expect(
      parseLegacyAttachments(
        'x\n\n--- attached file: big.log (205 KB) ---\nSaved to .yaah-attachments/big.log somewhere.',
      ),
    ).toBeNull()
  })

  it('renders ambiguous blobs (fences inside content) raw, silently', () => {
    // A message whose content itself contains a fence cannot be matched by
    // the exact template: two end-of-blob candidates exist and the parser
    // refuses the ambiguity (renders raw, exactly as today).
    const ambiguous = 'x\n\n--- attached file: tricky.txt ---\n```\ncode:\n```\nmore\n```'
    expect(parseLegacyAttachments(ambiguous)).toBeNull()
  })

  it('rejects a bare marker with no payload', () => {
    expect(parseLegacyAttachments('x\n\n--- attached file: empty.txt ---')).toBeNull()
  })

  it('pins the exact fixture strings the parser must recover (#144 ↔ backend)', () => {
    // The fixture pairs ARE the contract: every inline string the backend
    // produces (mirrored golden fixture) must parse back to its record.
    for (const [record, legacy] of FIXTURE) {
      const parsed = parseLegacyAttachments(legacy)
      expect(parsed).not.toBeNull()
      expect(parsed!.text).toBe('')
      expect(parsed!.attachments).toHaveLength(1)
      const [a] = parsed!.attachments
      expect(a.name).toBe(record.name)
      expect(a.path).toBe(record.path)
      expect(a.content).toBe(record.content)
    }
  })
})
