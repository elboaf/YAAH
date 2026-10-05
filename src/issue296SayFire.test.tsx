// Scheduled-fire spoken briefings (#296) — the frontend consumption half.
//
// The backend relay already puts {"type":"say","text":...} on the fire's
// tape; what was missing is everything downstream of it:
//
//   - tapeChunkForEvent returned null for say events, so the telemetry
//     tape never showed the briefing text;
//   - nothing on the scheduled path ever called the speech store, so
//     briefings were never spoken.
//
// Fix under test: AgentSayWatcher — an app-level component that polls the
// tape of every RUNNING agent conversation and feeds briefings into the
// existing SpeechPlayer queue, governed by the per-agent say_mode
// (#296 maintainer decision, default 'arrival'):
//
//   arrival — speak as the fire emits it, even when the chat is off screen;
//   visible — hold the briefing until that conversation becomes the
//             on-screen one, then speak it once.
//
// Dedupe ledger: say texts are processed at most once per conversation
// (poll resumption by offset is the first guard; the ledger is the
// belt-and-braces that survives a buffer re-fetch).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

const synthCalls: string[] = []
const tapeFetches: Array<{ cid: number; after: number }> = []

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
    getAgentTape: vi.fn(async (cid: number, after: number) => {
      tapeFetches.push({ cid, after })
      const events = scriptedTape.get(cid) ?? []
      // Resume semantics: only events past `after` come back.
      const fresh = events.slice(after)
      return { running: true, offset: events.length, events: fresh }
    }),
  }
})

import { AgentSayWatcher, tapeChunkForEvent, _resetFireSayLedgerForTests } from './components'
import { useAgent } from './store'
import { useTts } from './speech'
import type { ScheduledAgent } from './api'

const scriptedTape = new Map<number, Array<{ type: string; text?: string }>>()

function agentFixture(partial: Partial<ScheduledAgent>): ScheduledAgent {
  return {
    id: 'agent-x',
    workspace: 'C:/ws',
    name: 'nightly',
    prompt: 'p',
    schedule_type: 'interval',
    schedule_spec: { minutes: 30 },
    schedule_text: 'every 30m',
    approval_policy: 'sandbox-only',
    landing_mode: 'off',
    landing_branch: '',
    model: '',
    effort: '',
    memory_enabled: true,
    allow_ask_user: false,
    retention: 0,
    notify_on_success: false,
    enabled: true,
    running: false,
    instructions: [],
    chat_title: 'nightly',
    ...partial,
  } as ScheduledAgent
}

async function until(pred: () => boolean, ms = 4000): Promise<void> {
  const t0 = Date.now()
  while (!pred()) {
    if (Date.now() - t0 > ms) throw new Error('until(): condition not met')
    await new Promise((r) => setTimeout(r, 4))
  }
}

describe('fire say watcher (#296)', () => {
  beforeEach(() => {
    _resetFireSayLedgerForTests()
    scriptedTape.clear()
    tapeFetches.length = 0
    synthCalls.length = 0
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
      // The on-screen chat is conversation 5; agents run in 7 (arrival)
      // and 8 (visible) — both in the background unless stated otherwise.
      conversationId: 5,
      statusByConv: {},
      messagesByConv: {},
      pendingQuestions: {},
      pendingApprovals: {},
      pendingPlanApprovals: {},
      errorByConv: {},
      agents: [
        agentFixture({ id: 'a1', conversation_id: 7, running: true, say_mode: 'arrival' }),
        agentFixture({ id: 'a2', conversation_id: 8, running: true, say_mode: 'visible' }),
      ],
    })
  })

  afterEach(() => {
    cleanup()
    useTts.getState().stop()
  })

  it('speaks an arrival-mode briefing from a background fire', async () => {
    scriptedTape.set(7, [{ type: 'say', text: 'Nightly run finished, all green.' }])
    render(<AgentSayWatcher />)
    await until(() => synthCalls.some((t) => t.includes('Nightly run finished')))
  })

  it('speaks each fire briefing once — no re-speak across polls', async () => {
    scriptedTape.set(7, [{ type: 'say', text: 'Exactly once.' }])
    render(<AgentSayWatcher />)
    await until(() => synthCalls.some((t) => t.includes('Exactly once.')))
    // Well past a second poll tick: the briefing must not repeat.
    await new Promise((r) => setTimeout(r, 1300))
    expect(synthCalls.filter((t) => t.includes('Exactly once.')).length === 1).toBe(true)
  })

  it('holds a visible-mode briefing until the chat becomes the on-screen one', async () => {
    scriptedTape.set(8, [{ type: 'say', text: 'Held briefing.' }])
    render(<AgentSayWatcher />)
    await until(() => tapeFetches.some((f) => f.cid === 8))
    await new Promise((r) => setTimeout(r, 300))
    expect(synthCalls.some((t) => t.includes('Held briefing.'))).toBe(false)
    // Flip the selector to the fired agent's chat: the parked line speaks.
    useAgent.setState({ conversationId: 8 })
    await until(() => synthCalls.some((t) => t.includes('Held briefing.')))
  })

  it('speaks a visible-mode briefing immediately when its chat is on screen', async () => {
    scriptedTape.set(7, [{ type: 'say', text: 'On-screen fire.' }])
    useAgent.setState((s) => ({
      conversationId: 7,
      agents: s.agents.map((a) => (a.id === 'a1' ? { ...a, say_mode: 'visible' } : a)),
    }))
    render(<AgentSayWatcher />)
    await until(() => synthCalls.some((t) => t.includes('On-screen fire.')))
  })

  it('never polls when no agent is running', async () => {
    useAgent.setState((s) => ({ agents: s.agents.map((a) => ({ ...a, running: false })) }))
    render(<AgentSayWatcher />)
    await new Promise((r) => setTimeout(r, 300))
    expect(tapeFetches.length === 0).toBe(true)
  })

  it('renders say text on the telemetry tape — not a bare type tag', () => {
    const chunk = tapeChunkForEvent({ type: 'say', text: 'Tape-visible briefing.' } as never)
    expect(chunk).toContain('Tape-visible briefing.')
  })
})
