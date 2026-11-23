/** Test helpers for the Lexical-backed composer (#711).
 *
 * The composer renders a Lexical `contentEditable` div, not a `<textarea>`.
 * jsdom's `fireEvent.change` requires a native value setter (input/textarea/
 * select); a contentEditable div has none, so `fireEvent.change(box,
 * { target: { value } })` throws "The given element does not have a value
 * setter". The correct way to drive a Lexical editor in tests is its state
 * API: `editor.update(() => { $getRoot().append($createTextNode(...)) })`,
 * then fire a synthetic `input` event so the editor's `onChange` bridge
 * (registerTextContentListener -> parent setInput) runs.
 *
 * The contentEditable root exposes its editor on `__editor` (set by the
 * ComposerEditor component), so a helper can reach it from the queried box
 * without a ref.
 */
import { act } from '@testing-library/react'
import { fireEvent } from '@testing-library/react'
import { $getRoot, $createTextNode, $createParagraphNode } from 'lexical'

/** The editor instance attached to a contentEditable box. */
function editorOf(box: HTMLElement): { update: (fn: () => void) => void } | null {
  return (box as unknown as { __editor?: { update: (fn: () => void) => void } }).__editor ?? null
}

/** Drive the composer the way a user types, then let the send path read it.
 *
 * Sets the editor's text through its state API (wrapped in `act` so the
 * resulting `setInput` state update is flushed), then fires `input` so the
 * editor's `onChange` bridge notifies the parent. After this, the test may
 * fire `fireEvent.keyDown(box, { key: 'Enter' })` to send.
 */
export async function typeIntoComposer(box: HTMLElement, text: string): Promise<void> {
  const editor = editorOf(box)
  if (editor) {
    await act(async () => {
      await editor.update(() => {
        const root = $getRoot()
        root.clear()
        const para = $createParagraphNode()
        para.append($createTextNode(text))
        root.append(para)
      })
    })
    // The editor's onChange bridge reads the DOM textContent; fire input so
    // it runs and notifies the parent of the edit.
    fireEvent.input(box)
  } else {
    // Fallback (e.g. a stray textarea found by querySelector): set textContent.
    ;(box as unknown as { textContent: string }).textContent = text
  }
}
