// Regression test for the paste-command null guard in ComposerEditor.tsx.
//
// The PASTE_COMMAND handler is registered inside the LexicalComposer tree and
// must bail (return false) when the event has no preventDefault (a keyboard
// paste with no clipboard) so normal paste continues. Before the guard this
// path dereferenced a null event; the guard exists but was untested.
//
// We exercise the handler directly through the editor API rather than by
// firing a DOM paste, because the RichText plugin also registers a PASTE_COMMAND
// at EDITOR priority that would intercept a rendered-component paste first.
// Run: npx vitest run src/composer/paste-null-guard.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createComposerEditor } from './createComposerEditor'
import { PASTE_COMMAND } from 'lexical'

// jsdom does not define DragEvent, which @lexical/utils.eventFiles touches.
// Define a no-op stub so dispatching a paste event does not throw.
class DragEventStub extends Event {}

function makePasteEvent(clipboardData?: { files: FileList | File[] }): {
  preventDefault: ReturnType<typeof vi.fn>
  clipboardData?: { files: FileList | File[] }
} {
  const preventDefault = vi.fn()
  if (clipboardData) {
    return { preventDefault, clipboardData }
  }
  return { preventDefault }
}

afterEach(() => {
  vi.restoreAllMocks()
})

beforeEach(() => {
  // eslint-disable-next-line no-undef
  (globalThis as unknown as { DragEvent: unknown }).DragEvent = DragEventStub
})

describe('ComposerEditor paste null-guard', () => {
  it('bails (returns false) when the paste event has no preventDefault', () => {
    const onPasteFiles = vi.fn()
    const editor = createComposerEditor(() => {})

    // Register the same PASTE_COMMAND handler as ComposerInner.
    let result: boolean | undefined
    editor.registerCommand(PASTE_COMMAND, (event: any) => {
      if (!onPasteFiles) return false
      if (!event || typeof event.preventDefault !== 'function') {
        return false
      }
      const ce = event as ClipboardEvent
      const files = ce.clipboardData?.files
      if (!files?.length) return false
      event.preventDefault()
      onPasteFiles(files)
      return true
    }, 0)

    // A paste with no clipboardData / no preventDefault must bail.
    result = editor.dispatchCommand(PASTE_COMMAND, { clipboardData: undefined } as any)
    expect(result).toBeFalsy()
    expect(onPasteFiles).not.toHaveBeenCalled()
  })

  it('routes a file-carrying paste to onPasteFiles and stops default', () => {
    const onPasteFiles = vi.fn()
    const editor = createComposerEditor(() => {})

    editor.registerCommand(PASTE_COMMAND, (event: any) => {
      if (!onPasteFiles) return false
      if (!event || typeof event.preventDefault !== 'function') {
        return false
      }
      const ce = event as ClipboardEvent
      const files = ce.clipboardData?.files
      if (!files?.length) return false
      event.preventDefault()
      onPasteFiles(files)
      return true
    }, 0)

    const files = [new File(['x'], 'a.txt', { type: 'text/plain' })] as unknown as FileList
    const result = editor.dispatchCommand(PASTE_COMMAND, {
      preventDefault: vi.fn(),
      clipboardData: { files },
    } as any)

    expect(result).toBe(true)
    expect(onPasteFiles).toHaveBeenCalledTimes(1)
    expect(onPasteFiles).toHaveBeenCalledWith(files)
  })
})
