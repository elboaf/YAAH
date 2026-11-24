// Regression (#717): pressing the push-to-talk mic to dictate must cut active
// narration. The old release-time stop left the queue running across the whole
// press, so a long hold kept talking while the user was trying to speak. The
// fix stops narration at the moment the mic is taken (pttPress), not at
// release.
//
// This test drives the REAL ChatPanel + REAL SpeechPlayer (Web Audio and the
// synthesis api mocked, mirroring src/speech.test.ts and
// src/narrationQueue.test.tsx) through: narration playing, then the ptt-press
// window event, and pins ttsStop is called synchronously with the press.
//
// Run: npx vitest run src/pttStopsNarration.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, cleanup } from '@testing-library/react'

const synthCalls: string[] = []

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    ttsSynthesize: vi.fn(async (text: string) => {
      synthCalls.push(String(text))
      return new Blob([new ArrayBuffer(8)])
    }),
    ttsStop: vi.fn(async () => {}),
    ttsStatus: vi.fn(async () => ({ available: true, tts_enabled: true })),
    getConfig: vi.fn(async () => ({})),
    getContext: vi.fn(async () => ({ context_tokens: null, context_window: null, context_model: null })),
    getGitInfo: vi.fn(async () => ({ info: {} })),
    getConversation: vi.fn(async () => { throw new Error('unused') }),
    updateAgentModelEffort: vi.fn(async () => ({})),
    updateConversation: vi.fn(async () => ({})),
  }
})

import { ChatPanel } from './components'
import { useAgent } from './store'
import { useTts } from './speech'
import { ttsStop } from './api'

const ttsStopMock = vi.mocked(ttsStop)

async function until(pred: () => boolean, ms = 3000): Promise<void> {
  const t0 = Date.now()
  while (!pred()) {
    if (Date.now() - t0 > ms) throw new Error('until(): condition not met')
    await new Promise((r) => setTimeout(r, 4))
  }
}

describe('#717 ptt press stops narration', () => {
  beforeEach(() => {
    synthCalls.length = 0
    ttsStopMock.mockClear()
    // @ts-expect-error test stub
    globalThis.AudioContext = class {
      state = 'running'
      resume = async () => {}
      destination = {}
      createBufferSource() {
        const src: {
          buffer: null
          onended: (() => void) | null
          connect: () => void
          start: () => void
          stop: () => void
        } = {
          buffer: null,
          onended: null,
          connect: () => {},
          start: () => {},
          stop: () => {},
        }
        queueMicrotask(() => src.onended?.())
        return src
      }
      decodeAudioData = async () => ({}) as AudioBuffer
    }
    useTts.setState({ enabled: true, ready: true, speaking: false, speakingMsgId: null, error: null })
    useAgent.setState({
      conversationId: 7,
      statusByConv: { '7': 'thinking' },
      messagesByConv: {
        '7': [
          { id: 'u1', role: 'user', content: 'go' },
          { id: 'a1', role: 'assistant', content: 'Now narrating a long answer.' },
        ],
      },
      pendingQuestions: {},
      pendingApprovals: {},
      pendingPlanApprovals: {},
      errorByConv: {},
    })
  })

  afterEach(() => {
    cleanup()
    useTts.getState().stop()
  })

  it('dispatching ptt-press stops narration synchronously', async () => {
    render(<ChatPanel />)

    // Start narration: speak a message so a live utterance is playing.
    useTts.getState().speakMessage('a1', 'Now narrating a long answer.')
    await until(() => useTts.getState().speaking)

    // The mic is taken: this is the moment narration must be dropped.
    window.dispatchEvent(new CustomEvent('ptt-press'))

    // stop() floors the backend (ttsStop) — the whole lane is dropped, not
    // left running for the rest of the hold.
    await until(() => ttsStopMock.mock.calls.length > 0)
    expect(useTts.getState().speaking).toBe(false)
  })
})
