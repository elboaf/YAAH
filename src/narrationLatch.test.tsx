// Regression (#237, read-aloud speaks only the first <say> briefing of a turn
// and replays it on every chat switch):
//
//  A. The narration latch (n.said) lives in a COMPONENT ref, so a coalesced
//     one-message turn — where post-tool emissions append into the SAME msg.id
//     and each say OVERWRITES msg.say — early-returns after the first briefing
//     and every later briefing of the turn is silent.
//
//  B. On remount (chat switch away and back), the refs re-initialize with
//     said=false and the non-null msg.say is re-fed to the player — N chat
//     switches queue N duplicate reads.
//
// Fix under test: the "last say TEXT spoken for this msgId" latch survives
// remounts and is keyed per emission (say text), so both repros pass.
//
// Run: npx vitest run src/narrationLatch.test.tsx
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

describe('narration latch per emission, remount-surviving (#237)', () => {
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
    useAgent.setState({
      conversationId: 9,
      statusByConv: { '9': 'thinking' },
      messagesByConv: {
        '9': [
          { id: 'u1', role: 'user', content: 'go' },
          { id: 'a1', role: 'assistant', content: 'Working. Done now.', say: SAID_A },
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

  it('repro A: two sequential say cycles on ONE message id are BOTH spoken', async () => {
    render(<ChatPanel />)
    await until(() => synthCalls.some((t) => t.includes(SAID_A)))

    // Second emission of the same turn: same message id, content appended,
    // say OVERWRITTEN (last-emission-wins on the wire — handleStreamEvent).
    useAgent.setState((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        '9': [
          ...(s.messagesByConv['9'] ?? []).map((m) =>
            m.id === 'a1'
              ? { ...m, content: 'Working. Done now. More work completed.', say: SAID_B }
              : m,
          ),
        ],
      },
    }))

    await until(() => synthCalls.some((t) => t.includes(SAID_B)), 3000).catch(() => {
      throw new Error(`second briefing never synthesized; calls: ${JSON.stringify(synthCalls)}`)
    })
    expect(synthCalls.some((t) => t.includes(SAID_A))).toBe(true)
  })

  it('repro B: unmount + remount with unchanged msg.say re-feeds nothing', async () => {
    const view = render(<ChatPanel />)
    await until(() => synthCalls.some((t) => t.includes(SAID_A)))
    const countBefore = synthCalls.length

    view.unmount()
    render(<ChatPanel />)
    // Fresh objects, same content, STILL STREAMING (the chat-switch case:
    // the user navigates away mid-turn and back). The remounted panel's
    // narrate effect re-initializes its component refs (said=false), sees
    // the unchanged msg.say, and re-feeds the briefing; the fix must dedupe.
    useAgent.setState((s) => ({
      statusByConv: { ...s.statusByConv, '9': 'thinking' },
      messagesByConv: {
        ...s.messagesByConv,
        '9': [
          { id: 'u1', role: 'user' as const, content: 'go' },
          { id: 'a1', role: 'assistant' as const, content: 'Working. Done now.', say: SAID_A },
        ],
      },
    }))

    // Give the remounted effect plenty of chances to misbehave, then end the
    // turn: the run-end flush ends the open stream utterances, which lets
    // anything the remount queued (a duplicate briefing) actually synthesize.
    await new Promise((r) => setTimeout(r, 300))
    useAgent.setState((s) => ({ statusByConv: { ...s.statusByConv, '9': 'idle' } }))
    await new Promise((r) => setTimeout(r, 300))
    expect(synthCalls.slice(countBefore).some((t) => t.includes(SAID_A))).toBe(false)
  })
})
