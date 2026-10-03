// Issue #279 — streaming deltas must be coalesced behind requestAnimationFrame.
//
// Run: npx vitest run src/deltaBuffer.test.ts

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createDeltaBuffer } from './deltaBuffer'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** Fake rAF: capture callbacks so tests fire the frame explicitly. */
function stubRaf() {
  const frames: FrameRequestCallback[] = []
  vi.stubGlobal('requestAnimationFrame', vi.fn((cb: FrameRequestCallback) => {
    frames.push(cb)
    return frames.length
  }))
  const runFrames = () => {
    const pending = frames.splice(0)
    for (const cb of pending) cb(performance.now())
  }
  return { frames, runFrames }
}

describe('createDeltaBuffer', () => {
  it('writes synchronously (uncoalesced, still in order) without rAF', () => {
    // Simulate a non-browser env explicitly: jsdom PROVIDES rAF, so the
    // fallback must be exercised by stubbing it out. Without a frame
    // boundary the buffer degrades to one write per push — correctness
    // (all text, right message, in order) never depends on rAF existing.
    vi.stubGlobal('requestAnimationFrame', undefined)
    const writes: Array<{ id: string; text: string }> = []
    const buf = createDeltaBuffer((id, text) => writes.push({ id, text }))
    buf.push('m1', 'Hello ')
    buf.push('m1', 'world')
    // Each push carries its own incremental text (the store appends), so
    // the concatenated content is identical to the coalesced case.
    expect(writes).toEqual([
      { id: 'm1', text: 'Hello ' },
      { id: 'm1', text: 'world' },
    ])
  })

  it('coalesces multiple pushes into ONE write per frame', () => {
    const { runFrames } = stubRaf()
    const writes: Array<{ id: string; text: string }> = []
    const buf = createDeltaBuffer((id, text) => writes.push({ id, text }))

    buf.push('m1', 'a')
    buf.push('m1', 'b')
    buf.push('m1', 'c')
    expect(writes).toEqual([]) // nothing written yet

    runFrames()
    expect(writes).toEqual([{ id: 'm1', text: 'abc' }])
  })

  it('a msgId change flushes first so text never lands on the wrong message', () => {
    stubRaf()
    const writes: Array<{ id: string; text: string }> = []
    const buf = createDeltaBuffer((id, text) => writes.push({ id, text }))

    buf.push('m1', 'one ')
    buf.push('m2', 'two')
    expect(writes).toEqual([{ id: 'm1', text: 'one ' }])
  })

  it('flush is idempotent and cancels the pending frame', () => {
    const cancel = vi.fn()
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 42))
    vi.stubGlobal('cancelAnimationFrame', cancel)
    const writes: string[] = []
    const buf = createDeltaBuffer((_id, text) => writes.push(text))

    buf.push('m1', 'x')
    buf.flush()
    buf.flush()
    expect(writes).toEqual(['x'])
    expect(cancel).toHaveBeenCalledWith(42)
    // A flush with nothing buffered must not write again.
    buf.flush()
    expect(writes).toEqual(['x'])
  })

  it('push after flush schedules a fresh frame (buffer is reusable)', () => {
    const { runFrames } = stubRaf()
    const writes: string[] = []
    const buf = createDeltaBuffer((_id, text) => writes.push(text))

    buf.push('m1', 'a')
    runFrames()
    buf.push('m1', 'b')
    runFrames()
    expect(writes).toEqual(['a', 'b'])
  })
})
