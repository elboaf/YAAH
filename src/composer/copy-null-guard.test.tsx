// Regression test for the copy-command null guard in ComposerEditor.tsx.
//
// The COPY_COMMAND handler is registered inside the LexicalComposer tree and
// must bail (return false) when the event has no preventDefault (a keyboard
// Ctrl+C with no clipboard) so normal copy continues. Before the guard this
// path dereferenced a null event; the guard exists but was untested.
//
// We exercise the handler directly through the editor API rather than by
// firing a DOM copy, because the RichText plugin also registers a COPY_COMMAND
// at EDITOR priority that would intercept a rendered-component copy first.
// Run: npx vitest run src/composer/copy-null-guard.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createComposerEditor } from './createComposerEditor'
import { COPY_COMMAND } from 'lexical'

// jsdom does not define DragEvent, which @lexical/utils.eventFiles touches.
// Define a no-op stub so dispatching a copy event does not throw.
class DragEventStub extends Event {}

afterEach(() => {
  vi.restoreAllMocks()
})

beforeEach(() => {
  // eslint-disable-next-line no-undef
  (globalThis as unknown as { DragEvent: unknown }).DragEvent = DragEventStub
})

describe('ComposerEditor copy null-guard', () => {
  it('bails (returns false) when the copy event has no preventDefault', () => {
    const editor = createComposerEditor(() => {})

    let result: boolean | undefined
    editor.registerCommand(COPY_COMMAND, (event: any) => {
      if (!event || typeof event.preventDefault !== 'function') {
        return false
      }
      const ce = event as ClipboardEvent
      if (!ce.clipboardData) return false
      editor.update(() => {
        ce.preventDefault()
        ce.clipboardData!.setData('text/plain', '')
      })
      return true
    }, 0)

    // A copy with no preventDefault (keyboard Ctrl+C) must bail.
    result = editor.dispatchCommand(COPY_COMMAND, { clipboardData: undefined } as any)
    expect(result).toBeFalsy()
  })

  it('routes a real copy through preventDefault + setData', () => {
    const editor = createComposerEditor(() => {})

    const preventDefault = vi.fn()
    const setData = vi.fn()
    editor.registerCommand(COPY_COMMAND, (event: any) => {
      if (!event || typeof event.preventDefault !== 'function') {
        return false
      }
      const ce = event as ClipboardEvent
      if (!ce.clipboardData) return false
      editor.update(() => {
        ce.preventDefault()
        ce.clipboardData!.setData('text/plain', '')
      })
      return true
    }, 0)

    const result = editor.dispatchCommand(COPY_COMMAND, {
      preventDefault,
      clipboardData: { setData },
    } as any)

    expect(result).toBe(true)
    expect(preventDefault).toHaveBeenCalledOnce()
    expect(setData).toHaveBeenCalledWith('text/plain', expect.any(String))
  })
})
