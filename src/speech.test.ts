// #83 — the between-emissions process queue: a new emission must never cut
// the current utterance off mid-word; queued emissions play in arrival
// order; stop drops the whole pending lane. Web Audio is mocked (jsdom has
// no AudioContext); the backend synthesis is mocked at the api boundary.
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./api', () => ({
  ttsStop: vi.fn(),
  ttsSynthesize: vi.fn(),
}))

import { ttsStop, ttsSynthesize } from './api'
import { SpeechPlayer } from './speech'

const ttsStopMock = vi.mocked(ttsStop)
const synthMock = vi.mocked(ttsSynthesize)

// Fake audio graph: a source is "playing" between start() and the moment
// the test releases it (releaseCurrent -> onended -> play() resolves).
let live: { onended: (() => void) | null }[] = []
function playingCount(): number {
  return live.length
}
function releaseCurrent(): void {
  const old = live
  live = []
  for (const s of old) s.onended?.()
}
class FakeAudioContext {
  state = 'running'
  resume = async () => {}
  destination = {}
  createBufferSource() {
    const src = {
      buffer: null as AudioBuffer | null,
      onended: null as (() => void) | null,
      connect: () => {},
      start: () => {
        live.push(src)
      },
      stop: () => {
        // Hard stop: kill without firing onended listeners added later.
        live = live.filter((s) => s !== src)
        src.onended = null
      },
    }
    return src
  }
  decodeAudioData = async () => ({}) as AudioBuffer
}

beforeEach(() => {
  vi.clearAllMocks()
  live = []
  synthMock.mockImplementation(async () => new Blob([new ArrayBuffer(8)]))
  // @ts-expect-error test stub
  globalThis.AudioContext = FakeAudioContext
})

async function until(pred: () => boolean, ms = 1000): Promise<void> {
  const t0 = Date.now()
  while (!pred()) {
    if (Date.now() - t0 > ms) throw new Error('until(): condition not met')
    await new Promise((r) => setTimeout(r, 2))
  }
}

describe('#83 speech process queue', () => {
  it('an emission arriving mid-utterance does not cut the current audio; it plays after', async () => {
    const p = await fresh()
    void p.speak('a', ['a1', 'a2'])
    await until(() => playingCount() === 1) // a1 mid-word
    expect(p.current.speaking).toBe(true)
    expect(p.current.msgId).toBe('a')

    // Second emission arrives while a1 is playing.
    void p.speak('b', ['b1'])

    // a1's audio was NOT stopped; b1 not synthesized yet (still queued).
    // (a2's prefetch may already be in flight — that's the one-chunk
    // prefetch inside the utterance, also by design.)
    expect(playingCount()).toBe(1)
    const callsNow = synthMock.mock.calls.map((c) => c[0])
    expect(callsNow[0]).toBe('a1')
    expect(callsNow).not.toContain('b1')
    releaseCurrent() // a1 ends -> a2 plays (still not b1)
    await until(() => synthMock.mock.calls.some((c) => c[0] === 'a2'))
    expect(synthMock.mock.calls.some((c) => c[0] === 'b1')).toBe(false)
    await until(() => playingCount() === 1)
    releaseCurrent() // a2 ends -> b's turn
    await until(() => synthMock.mock.calls.some((c) => c[0] === 'b1'))
    expect(p.current.msgId).toBe('b')
    releaseCurrent()
    await until(() => !p.current.speaking)
  })

  it('rapid back-to-back emissions are synthesized in arrival order (FIFO, none skipped)', async () => {
    const p = await fresh()
    const order: string[] = []
    synthMock.mockImplementation(async (text: string) => {
      order.push(text)
      return new Blob([new ArrayBuffer(8)])
    })
    void p.speak('e0', ['c0'])
    void p.speak('e1', ['c1'])
    void p.speak('e2', ['c2'])
    void p.speak('e3', ['c3'])
    for (let i = 0; i < 4; i++) {
      await until(() => order.length >= i + 1)
      await until(() => playingCount() === 1)
      releaseCurrent()
      await until(() => playingCount() === 0)
    }
    expect(order).toEqual(['c0', 'c1', 'c2', 'c3'])
    // speaking flips false a few microtasks after the last onended —
    // poll for it instead of asserting synchronously.
    await until(() => !p.current.speaking)
  })

  it('stop() kills current audio AND drops the whole pending lane', async () => {
    const p = await fresh()
    void p.speak('a', ['a1'])
    await until(() => playingCount() === 1)
    void p.speak('b', ['b1'])
    void p.speak('c', ['c1'])
    p.stop(0)
    expect(p.current.speaking).toBe(false)
    expect(playingCount()).toBe(0) // current audio killed
    const callsAfter = synthMock.mock.calls.length
    releaseCurrent()
    await new Promise((r) => setTimeout(r, 40))
    expect(synthMock.mock.calls.length).toBe(callsAfter) // nothing new synthesized
    expect(synthMock.mock.calls.some((c) => c[0] === 'b1' || c[0] === 'c1')).toBe(false)
    expect(p.current.speaking).toBe(false)
  })

  it('deferred beginStream buffers appends and plays them after the current utterance', async () => {
    const p = await fresh()
    void p.speak('a', ['a1'])
    await until(() => playingCount() === 1)
    // New emission mid-utterance begins a stream.
    const feed = p.beginStream('b')
    feed.append('b1')
    feed.append('b2')
    feed.end()
    // Still busy with a1: nothing from b synthesized yet.
    expect(synthMock.mock.calls.some((c) => (c[0] as string).startsWith('b'))).toBe(false)
    releaseCurrent()
    // a's loop ends; b's buffered chunks replay in order.
    await until(() => synthMock.mock.calls.some((c) => c[0] === 'b1'))
    expect(p.current.msgId).toBe('b')
    await until(() => playingCount() === 1)
    releaseCurrent() // b1 done -> b2
    await until(() => synthMock.mock.calls.some((c) => c[0] === 'b2'))
    await until(() => playingCount() === 1)
    releaseCurrent()
    // feed was already end()ed while queued; after b2 the utterance
    // completes on its own.
    await until(() => !p.current.speaking)
    expect(playingCount()).toBe(0)
  })

  it('an emission arriving after the queue drained starts immediately', async () => {
    const p = await fresh()
    void p.speak('a', ['a1'])
    await until(() => playingCount() === 1)
    releaseCurrent()
    await until(() => !p.current.speaking)
    void p.speak('b', ['b1'])
    await until(() => synthMock.mock.calls.some((c) => c[0] === 'b1'))
    expect(p.current.msgId).toBe('b')
    await until(() => playingCount() === 1)
    releaseCurrent()
    await until(() => !p.current.speaking)
  })

  it('verbatim flush into the old stream plays BEFORE the next emission starts', async () => {
    const p = await fresh()
    const feedA = p.beginStream('a')
    feedA.append('a1')
    await until(() => playingCount() === 1)
    // Emission swap without a say tag: the effect flushes held verbatim
    // sentences into the OLD feed, end()s it, then begins the new one.
    feedA.append('a-flush')
    feedA.end()
    const feedB = p.beginStream('b')
    feedB.append('b1')
    // a1 is still playing untouched; flush + b1 are queued in order.
    expect(synthMock.mock.calls.map((c) => c[0])).toEqual(['a1'])
    releaseCurrent()
    await until(() => synthMock.mock.calls.some((c) => c[0] === 'a-flush'))
    await until(() => playingCount() === 1)
    releaseCurrent()
    await until(() => synthMock.mock.calls.some((c) => c[0] === 'b1'))
    await until(() => playingCount() === 1)
    feedB.end()
    const calls = synthMock.mock.calls.map((c) => c[0])
    expect(calls.indexOf('a-flush')).toBeLessThan(calls.indexOf('b1'))
    releaseCurrent()
    await until(() => !p.current.speaking)
    expect(ttsStopMock).toHaveBeenCalled()
  })
})

// The player is a singleton, but tests hand-build a fresh instance per
// test so no closed-over queue/generation state leaks between cases
// (module-reset + dynamic re-import deadlocks the vitest 5 worker).
function fresh(): SpeechPlayer {
  return new SpeechPlayer()
}
