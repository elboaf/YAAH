import { describe, expect, it } from 'vitest'
import { classifyDrop, dropAdvertisesFiles } from './dropFiles'

/**
 * Issue #48: dropping a file/image onto the chat window did nothing. The
 * composer's HTML5 onDrop existed, but on Windows Tauri's native OLE drag-
 * drop handler intercepts OS file drags before the webview sees them, so
 * `drop` never fired. The fix restores `dragDropEnabled: false` in
 * tauri.conf.json so HTML5 drop events reach the webview.
 *
 * These tests pin the payload-classification logic the drop handler now runs
 * through — including the one signature that must never vanish silently: a
 * drag that advertises files but yields no readable File objects.
 */

function dt(
  opts: { files?: File[]; types?: string[] } = {},
): DataTransfer {
  const d = {
    files: opts.files ?? [],
    types: opts.types ?? (opts.files?.length ? ['Files'] : []),
  } as unknown as DataTransfer
  return d
}

const image = new File(['x'], 'shot.png', { type: 'image/png' })
const text = new File(['hello'], 'notes.txt', { type: 'text/plain' })

describe('classifyDrop (#48)', () => {
  it('routes readable files through as the happy path', () => {
    const out = classifyDrop(dt({ files: [image, text] }))
    expect(out).toEqual({ kind: 'files', files: [image, text] })
  })

  it('passes a single file through unchanged', () => {
    const out = classifyDrop(dt({ files: [text] }))
    expect(out.kind).toBe('files')
    if (out.kind === 'files') expect(out.files).toHaveLength(1)
  })

  it('treats a drag with no payload as empty (no feedback noise)', () => {
    expect(classifyDrop(dt())).toEqual({ kind: 'empty' })
    expect(classifyDrop(null)).toEqual({ kind: 'empty' })
  })

  it('marks text-only drags as empty', () => {
    expect(classifyDrop(dt({ types: ['text/plain'] }))).toEqual({ kind: 'empty' })
  })

  it('detects the silent-failure signature: Files advertised, none readable', () => {
    // The WebView2 partial-failure mode: the native layer intercepts the
    // drop, the browser event fires, but dataTransfer.files arrives empty.
    expect(classifyDrop(dt({ types: ['Files'] }))).toEqual({
      kind: 'files-but-inaccessible',
    })
  })

  it('does not claim inaccessibility when files are readable', () => {
    expect(dropAdvertisesFiles(dt({ files: [image] }))).toBe(true)
    expect(dropAdvertisesFiles(dt({ types: ['Files'] }))).toBe(true)
    expect(dropAdvertisesFiles(dt())).toBe(false)
    expect(dropAdvertisesFiles(null)).toBe(false)
  })

  it('tolerates a hostile dataTransfer (throwing types getter)', () => {
    const hostile = {
      files: [],
      get types() {
        throw new Error('boom')
      },
    } as unknown as DataTransfer
    expect(classifyDrop(hostile)).toEqual({ kind: 'empty' })
  })
})
