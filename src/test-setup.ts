import '@testing-library/jest-dom/vitest'

// jsdom does not implement the Drag Events spec, so `DragEvent` is undefined
// in the test environment. The composer's drop handler reads e.dataTransfer off
// a DragEvent; absent a polyfill that path throws ReferenceError, which escapes
// as an uncaught exception and fails the whole vitest run even though the test
// itself passes (the composer catches the error). Provide it as an Event
// subclass carrying the drag properties the composer reads.
if (typeof globalThis.DragEvent === 'undefined') {
  class DragEventPolyfill extends Event {
    dataTransfer: unknown = null
    clientX = 0
    clientY = 0
  }
  globalThis.DragEvent = DragEventPolyfill as unknown as typeof DragEvent
}

// jsdom's Range has no getBoundingClientRect(), but Lexical's caret/selection
// code calls it (getCaretRect → $updateDOMSelection) on every commit. Absent a
// polyfill that throws TypeError as an uncaught exception and fails the whole
// vitest run even though the asserting test passes. Return a zero rect — the
// composer tests don't care about geometry, only that the call resolves.
if (typeof globalThis.Range !== 'undefined' && typeof globalThis.Range.prototype.getBoundingClientRect !== 'function') {
  globalThis.Range.prototype.getBoundingClientRect = function () {
    return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 } as DOMRect
  }
}
