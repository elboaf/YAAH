// Issue #279: coalesce streaming text deltas behind requestAnimationFrame.
//
// Model streams arrive as NDJSON chunks, often several per painted frame;
// each appendTextDelta is a store write -> subscription fan-out -> render
// pass. Buffering to the next animation frame keeps the painted output
// IDENTICAL (all text lands, in order, one appendData per frame) while
// collapsing the store-write rate to at most one per frame. The terminal
// paths (done / stopped / abort / steer) flush explicitly so buffered text
// can never land after final state.

export interface DeltaBuffer {
  /** Buffer a delta for `msgId`. A msgId change flushes first, so text
   *  never lands on the wrong message. */
  push(msgId: string, text: string): void
  /** Write any buffered text now (idempotent; cancels a pending frame). */
  flush(): void
}

export function createDeltaBuffer(
  append: (msgId: string, text: string) => void,
): DeltaBuffer {
  let buf: { msgId: string; text: string } | null = null
  let scheduled: number | null = null
  const flush = () => {
    if (scheduled !== null && typeof cancelAnimationFrame === 'function') {
      cancelAnimationFrame(scheduled)
    }
    scheduled = null
    if (!buf) return
    append(buf.msgId, buf.text)
    buf = null
  }
  return {
    push(msgId, text) {
      if (buf && buf.msgId !== msgId) flush()
      buf = { msgId, text: (buf?.text ?? '') + text }
      if (scheduled === null) {
        // Non-browser environments (tests without rAF) flush synchronously:
        // correctness never depends on the frame boundary.
        if (typeof requestAnimationFrame === 'function') {
          scheduled = requestAnimationFrame(flush)
        } else {
          flush()
        }
      }
    },
    flush,
  }
}
