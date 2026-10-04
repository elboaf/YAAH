// Regression (#291): steering a running chat makes read-aloud re-speak the
// ENTIRE pre-steer message verbatim before the post-steer briefing plays.
//
// The narrate effect's emission-swap branch (different msgId mid-run) flushes
// the previous emission's held sentences with no `prev.said` guard, while the
// run-end flush guards with `!n.said && (... || !wasSaySpoken(...))`. When the
// previous emission WAS narrated via its <say> briefing (said=true, spoken
// stays 0), the unguarded flush re-feeds the whole message content verbatim
// on top of the already-spoken briefing. Two real paths swap msgId mid-run:
//
//  A. Steer (#7): startAssistantEmissionAfterUser opens a new assistant
//     message after the injected user row; the pre-steer message holds the
//     whole log so far.
//  B. exit_plan approval split: splitAtPlanApproval splices a fresh assistant
//     message after the planning emission once the plan is approved.
//
// Pins, per case: the new emission's briefing is synthesized, NO chunk
// containing the previous message's verbatim-only prose is synthesized, and
// the previous briefing is not re-synthesized either.
//
// Run: npx vitest run src/steerSayReplay.test.tsx
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

import { ChatPanel, _resetNarrationDedupeForTests } from './components'
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

const SAID_A = 'Briefing alpha.'
const SAID_B = 'Briefing beta.'
const SAID_P = 'Plan is ready for review.'
const SAID_X = 'Plan approved, executing now.'

// A mid-body sentence of the pre-swap message's content: it can only reach
// the synthesizer through the unguarded verbatim flush, never through a
// briefing (spokenLine feeds msg.say alone while the tag exists).
const A_VERBATIM_ONLY = 'Alpha prose two'
const P_VERBATIM_ONLY = 'Planning prose two'

describe('emission swap after a say-narrated emission (#291)', () => {
  beforeEach(() => {
    _resetNarrationDedupeForTests()
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
  })

  afterEach(() => {
    cleanup()
    useTts.getState().stop()
  })

  it('steer: the swap after a say-narrated message re-speaks nothing verbatim', async () => {
    useAgent.setState({
      conversationId: 9,
      statusByConv: { '9': 'thinking' },
      messagesByConv: {
        '9': [
          { id: 'u1', role: 'user', content: 'go' },
          {
            id: 'a1',
            role: 'assistant',
            content: 'Alpha prose one. Alpha prose two. Alpha prose three.',
            say: SAID_A,
          },
        ],
      },
      pendingQuestions: {},
      pendingApprovals: {},
      pendingPlanApprovals: {},
      errorByConv: {},
    })
    render(<ChatPanel />)
    await until(() => synthCalls.some((t) => t.includes(SAID_A)))

    // The steer lands mid-run: a user row is injected, then a NEW assistant
    // message opens after it (startAssistantEmissionAfterUser) — the msgId
    // swap the narrate effect must survive without a verbatim replay.
    useAgent.setState((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        '9': [
          ...(s.messagesByConv['9'] ?? []),
          { id: 'u2', role: 'user' as const, content: 'actually, steer instead' },
          { id: 'a2', role: 'assistant' as const, content: 'Beta prose one. Beta prose two.', say: SAID_B },
        ],
      },
    }))

    await until(() => synthCalls.some((t) => t.includes(SAID_B)), 3000).catch(() => {
      throw new Error(`post-steer briefing never synthesized; calls: ${JSON.stringify(synthCalls)}`)
    })
    expect(synthCalls.some((t) => t.includes(A_VERBATIM_ONLY))).toBe(false)
    expect(synthCalls.filter((t) => t.includes(SAID_A)).length).toBe(1)
  })

  it('exit_plan split: the planning message is not verbatim-replayed after approval', async () => {
    useAgent.setState({
      conversationId: 9,
      statusByConv: { '9': 'thinking' },
      messagesByConv: {
        '9': [
          { id: 'u1', role: 'user', content: 'plan it' },
          {
            id: 'a1',
            role: 'assistant',
            content: 'Planning prose one. Planning prose two.',
            say: SAID_P,
            toolCalls: [
              {
                id: 't1',
                name: 'exit_plan',
                args: { plan: '# The plan' },
                result: { decision: 'approved' },
              },
            ],
          },
        ],
      },
      pendingQuestions: {},
      pendingApprovals: {},
      pendingPlanApprovals: {},
      errorByConv: {},
    })
    render(<ChatPanel />)
    await until(() => synthCalls.some((t) => t.includes(SAID_P)))

    // splitAtPlanApproval already ran its splice (approved above): the
    // execution emission opens as a NEW message id mid-run — same swap path.
    useAgent.setState((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        '9': [
          ...(s.messagesByConv['9'] ?? []),
          { id: 'a2', role: 'assistant' as const, content: 'Execute prose one. Execute prose two.', say: SAID_X },
        ],
      },
    }))

    await until(() => synthCalls.some((t) => t.includes(SAID_X)), 3000).catch(() => {
      throw new Error(`execution briefing never synthesized; calls: ${JSON.stringify(synthCalls)}`)
    })
    expect(synthCalls.some((t) => t.includes(P_VERBATIM_ONLY))).toBe(false)
    expect(synthCalls.filter((t) => t.includes(SAID_P)).length).toBe(1)
  })
})
