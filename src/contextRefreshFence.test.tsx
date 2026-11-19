// #1245 / CodeRabbit #768: the context re-reads must never let a LATE
// response overwrite a NEWER update.
//
// Three places re-read the context endpoint asynchronously: the `done`
// handler (settle the chip at turn end), the `error`/`stopped` handlers
// (same settle for a turn that did not complete), and the usage tick's
// window-resolution fallback. All are async, so a slow answer can resolve
// AFTER a newer turn's usage tick -- and the old code wrote it
// unconditionally, so the chip would jump back to a stale cost/token count.
//
// This drives the REAL Composer + real streamAgentTurn + real store, holds
// the context response open, then lets a newer usage tick land first and
// asserts the late response does NOT clobber it.
//
// Run: npx vitest run src/contextRefreshFence.test.tsx
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { useAgent } from './store'
import { Composer, ChatPanel } from './components'

const { getConfig, createConversation, listConversations, getMessages, getAgentTape } =
  vi.hoisted(() => ({
    getConfig: vi.fn(),
    createConversation: vi.fn(),
    listConversations: vi.fn(),
    getMessages: vi.fn(),
    getAgentTape: vi.fn(),
  }))

// Mock everything EXCEPT streamAgentTurn (and getContext, which we control
// per-test by holding its response open) so the stream seam stays real.
vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  getConfig,
  createConversation,
  listConversations,
  getMessages,
  getAgentTape,
}))

/** A JSON Response with the content-type the real API helper requires --
 * without it `api()` treats the body as a non-JSON page and throws. */
function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

function ndjsonResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c))
      controller.close()
    },
  })
  return new Response(stream, { status: 200 })
}

/** Deferred context responses: resolve them by hand, in any order. */
let pendingContext: Array<{
  resolve: (v: unknown) => void
  body: { context_tokens: number; context_window: number | null; context_model: string | null; cost_spent: number | null; completion_tokens_total: number | null }
}> = []

function screen_textarea(): HTMLElement {
  return document.querySelector('textarea') as unknown as HTMLElement
}

describe('#1245 context refresh is generation-fenced', () => {
  let fetchMock: ReturnType<typeof vi.spyOn>
  let currentScript = ''

  beforeEach(() => {
    vi.restoreAllMocks()
    pendingContext = []
    getConfig.mockResolvedValue({})
    createConversation.mockResolvedValue({ id: 42 })
    listConversations.mockResolvedValue([])
    getMessages.mockResolvedValue([])
    getAgentTape.mockResolvedValue({ events: [], next: 0 })
    useAgent.setState({
      conversationId: null,
      statusByConv: {},
      messagesByConv: {},
      pendingQuestions: {},
      pendingApprovals: {},
      pendingPlanApprovals: {},
      contextByConv: {},
    })
    fetchMock = vi.spyOn(window, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
      const u = String(input)
      if (u.includes('/api/agent/')) return ndjsonResponse([currentScript])
      if (u.includes('/context')) {
        // Hold this response open; the test resolves it by hand.
        return new Promise<Response>((resolve) => {
          pendingContext.push({
            resolve: (v) => resolve(jsonResponse(v)),
            body: {
              context_tokens: 0,
              context_window: null,
              context_model: null,
              cost_spent: null,
              completion_tokens_total: null,
            },
          })
        })
      }
      return new Response(JSON.stringify([]), { status: 200 })
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('a late context re-read does not overwrite a newer usage tick', async () => {
    // Turn 1: usage tick sets cost 0.50 and a known window, then done fires
    // the (held) context re-read.
    currentScript =
      '{"type":"usage","usage_tokens":1000,"model":"m","cost_spent":0.50}\n' +
      '{"type":"done"}\n'
    render(<Composer />)
    const box = screen_textarea()
    fireEvent.change(box, { target: { value: 'first' } })
    fireEvent.keyDown(box, { key: 'Enter' })

    await waitFor(() => {
      expect(useAgent.getState().contextByConv['42']?.cost).toBeCloseTo(0.5)
    })
    // Seed the sticky window so turn 2 takes the no-refetch path.
    await waitFor(() => expect(pendingContext.length).toBeGreaterThan(0))
    // Turn 1's own in-flight requests: these are the ones that will resolve
    // LATE, with the older total.
    const staleRequests = pendingContext.splice(0)

    // Turn 2: a newer usage tick raises the session total to 0.60.
    currentScript =
      '{"type":"usage","usage_tokens":2000,"model":"m","cost_spent":0.60}\n' +
      '{"type":"done"}\n'
    fireEvent.change(box, { target: { value: 'second' } })
    fireEvent.keyDown(box, { key: 'Enter' })

    await waitFor(() => {
      expect(useAgent.getState().contextByConv['42']?.cost).toBeCloseTo(0.6)
    })

    // Now turn 1's held responses finally resolve with the OLDER total.
    // They must be discarded: a late answer may never undo a newer update.
    for (const p of staleRequests) {
      p.resolve({
        context_tokens: 1000,
        context_window: 200000,
        context_model: 'm',
        cost_spent: 0.50,
        completion_tokens_total: 10,
      })
    }
    await new Promise((r) => setTimeout(r, 50))

    const ctx = useAgent.getState().contextByConv['42']
    expect(ctx?.cost).toBeCloseTo(0.6) // newer value survives
    expect(ctx?.tokens).toBe(2000)
  })

  it('the mount-time context load cannot overwrite a newer usage tick', async () => {
    // Open an EXISTING conversation whose window is already known: the
    // sticky-window path makes the turn's usage tick write through the
    // store without a second context read, so the only held read is the
    // mount effect's own.
    useAgent.setState({
      conversationId: 42,
      contextByConv: {
        '42': { tokens: 100, window: 200000, model: 'm', cost: 0.1, completionTokens: 0 },
      },
    })
    render(<ChatPanel />)
    await waitFor(() => expect(pendingContext.length).toBeGreaterThan(0))
    const staleMountReads = pendingContext.splice(0)

    // A turn lands while the mount read is still in flight: its usage tick
    // raises the session total to 0.60 in the store.
    currentScript =
      '{"type":"usage","usage_tokens":2000,"model":"m","cost_spent":0.60}\n' +
      '{"type":"done"}\n'
    const box = screen_textarea()
    fireEvent.change(box, { target: { value: 'hello' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => {
      expect(useAgent.getState().contextByConv['42']?.cost).toBeCloseTo(0.6)
    })

    // The mount read finally answers with the OLDER total. It must be
    // discarded: the tick that landed while it was in flight is newer.
    for (const p of staleMountReads) {
      p.resolve({
        context_tokens: 100,
        context_window: 200000,
        context_model: 'm',
        cost_spent: 0.1,
        completion_tokens_total: 0,
      })
    }
    await new Promise((r) => setTimeout(r, 50))

    const ctx = useAgent.getState().contextByConv['42']
    expect(ctx?.cost).toBeCloseTo(0.6) // newer tick survives
    expect(ctx?.tokens).toBe(2000)
  })
})
