// Golden fixture (#142/#144): mirrored in backend/tests/test_attachment_inline.py.
// The canonical attachment records paired with their EXACT legacy inline
// strings — the format the backend re-inlines into model context and the
// display parser (parseLegacyAttachments) matches for old rows. The two
// sides must never drift.
import type { Attachment } from './attachments'

export const FIXTURE: Array<[Attachment, string]> = [
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
