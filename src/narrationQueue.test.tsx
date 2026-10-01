// Regression (voice symptom 1, "only the first <say> emission is spoken"):
// two failure modes meet at the ChatPanel narration effect's emission swap.
//
//  A. The swap never calls prev.feed.end(): the first emission's stream
//     utterance never leaves the player's `while (isStream && !done)` wait,
//     so the process queue never drains and every later emission queues
//     behind it forever — the voice goes silent after the first turn.
//
//  B. Both "flush the held sentences" loops append from index n.spoken, but
//     the hold path already set n.spoken to the FULL sentence count — so the
//     fallback flush appends nothing and an emission without a <say> tag is
//     never spoken at all.
//
// This test drives the REAL ChatPanel + REAL SpeechPlayer (Web Audio and the
// synthesis api mocked, mirroring src/speech.test.ts) through a two-emission
// turn and pins: later emissions are synthesized, and fallback text plays.
//
// Run: npx vitest run src/narrationQueue.test.tsx
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
import { ttsSynthesize } from './api'

const synthMock = vi.mocked(ttsSynthesize)

async function until(pred: () => boolean, ms = 3000): Promise<void> {
  const t0 = Date.now()
  while (!pred()) {
    if (Date.now() - t0 > ms) throw new Error('until(): condition not met')
    await new Promise((r) => setTimeout(r, 4))
  }
}

const E1 = 'First emission answer one. Second sentence of emission one.'
const E2 = 'Second emission answer two. Another sentence of emission two.'

describe('narration across emissions (#symptom1)', () => {
  beforeEach(() => {
    synthCalls.length = 0
    synthMock.mockClear()
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
        // Resolve immediately: chunks "play" instantly.
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
          { id: 'a1', role: 'assistant', content: E1 },
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
    // Kill any live utterance so a red run's stalled stream loop does not
    // leak into the next test (same contamination the user hears live).
    useTts.getState().stop()
  })

  it('speaks every briefing: later emissions are not stuck behind the first', async () => {
    render(<ChatPanel />)

    // Emission 1 ends unbriefed; emission 2 streams in and gets a briefing.
    useAgent.setState((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        '7': [
          ...(s.messagesByConv['7'] ?? []).filter((m) => m.id !== 'a2'),
          { id: 'a2', role: 'assistant' as const, content: E2 },
        ],
      },
    }))
    useAgent.getState().setSay('7', 'a2', 'Second briefing here.')

    await until(() => synthCalls.some((t) => t.includes('Second briefing')), 3000)
      .catch(() => {
        throw new Error(
          `second emission was never synthesized; ` +
            `synth calls were: ${JSON.stringify(synthCalls)}`,
        )
      })
  })

  it('flushes held sentences when an emission ends without a <say> tag', async () => {
    render(<ChatPanel />)

    // Emission 1 never receives a briefing; emission 2 arrives (swap) and is
    // briefed. Emission 1's held sentences must still be synthesized.
    useAgent.setState((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        '7': [
          ...(s.messagesByConv['7'] ?? []),
          { id: 'a2', role: 'assistant' as const, content: E2 },
        ],
      },
    }))
    useAgent.getState().setSay('7', 'a2', 'Second briefing here.')

    await until(() => synthCalls.some((t) => t.includes('First emission answer one')), 3000)
      .catch(() => {
        throw new Error(`emission-1 fallback was never flushed; synth calls: ${JSON.stringify(synthCalls)}`)
      })
  })
})
