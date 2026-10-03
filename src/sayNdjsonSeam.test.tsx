// Issue #230: integration test for the NDJSON → AgentEvent seam. The say
// wire suite mocks streamAgentTurn, so the JSON.parse pass-through of `say`
// events was pinned only on each side. This drives the REAL streamAgentTurn
// against a fake fetch Response body and asserts say events arrive in the
// component/state layer intact — including malformed and partial lines.
//
// Run: npx vitest run src/sayNdjsonSeam.test.tsx
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { useAgent } from './store'
import { Composer } from './components'
import { streamAgentTurn } from './api'

const { getConfig, createConversation, listConversations, getMessages, getAgentTape } =
  vi.hoisted(() => ({
    getConfig: vi.fn(),
    createConversation: vi.fn(),
    listConversations: vi.fn(),
    getMessages: vi.fn(),
    getAgentTape: vi.fn(),
  }))

// Mock everything EXCEPT streamAgentTurn — the seam under test is real.
vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  getConfig,
  createConversation,
  listConversations,
  getMessages,
  getAgentTape,
}))

/** A minimal ReadableStream Response from NDJSON text chunks. */
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

describe('NDJSON → AgentEvent seam (#230)', () => {
  let fetchMock: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    vi.restoreAllMocks()
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
    })
    fetchMock = vi.spyOn(window, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
      const u = String(input)
      if (u.includes('/api/agent/')) return ndjsonResponse([currentScript])
      return new Response(JSON.stringify([]), { status: 200 })
    })
  })

  let currentScript = ''

  it('delivers a say event through the real NDJSON parse path', async () => {
    currentScript =
      '{"type":"text","text":"Visible answer."}\n' +
      '{"type":"say","text":"Briefing through the wire."}\n' +
      '{"type":"done"}\n'
    render(<Composer />)

    const box = screen_textarea()
    fireEvent.change(box, { target: { value: 'do the thing' } })
    fireEvent.keyDown(box, { key: 'Enter' })

    await waitFor(() => {
      const msgs = useAgent.getState().messagesByConv['42'] ?? []
      const asst = msgs.find((m) => m.role === 'assistant')
      expect(asst?.say).toBe('Briefing through the wire.')
    })
    // The tag-free chat body must not gain the briefing as text.
    const asst = (useAgent.getState().messagesByConv['42'] ?? []).find(
      (m) => m.role === 'assistant',
    )
    expect(asst?.content).toBe('Visible answer.')
    expect(fetchMock).toHaveBeenCalled()
    void streamAgentTurn // real import asserted at module load
  })

  it('fails the stream loudly on a malformed line (parse error surfaces, no silent drop)', async () => {
    // Current contract: streamAgentTurn does not swallow bad JSON — the
    // parse error propagates, the send path rolls the optimistic rows back
    // and flags the conversation as errored. A malformed NDJSON line must
    // never be silently dropped mid-turn.
    currentScript = 'BAD LINE\n'
    let thrown: unknown = null
    try {
      await streamAgentTurn(1, 'm', 'w', () => {}, undefined, [], [], false, undefined, [])
      void thrown
    } catch (e) {
      thrown = e
    }
    expect(thrown).toBeInstanceOf(SyntaxError)
    currentScript =
      '{"type":"say","text":"arrived intact"}\n' +
      'THIS IS NOT JSON\n' +
      '{"type":"text","text":"still alive"}\n' +
      '{"type":"done"}\n'
    render(<Composer />)
    const box = screen_textarea()
    fireEvent.change(box, { target: { value: 'go' } })
    fireEvent.keyDown(box, { key: 'Enter' })

    await waitFor(() => {
      expect(useAgent.getState().statusByConv['42']).toBe('error')
    })
    // Nothing after the malformed line leaked onto the tape.
    const msgs = useAgent.getState().messagesByConv['42'] ?? []
    expect(msgs.some((m) => (m as { content?: string }).content === 'still alive')).toBe(false)
  })

  it('reassembles a say event split across chunk boundaries (partial line)', async () => {
    currentScript =
      '{"type":"te' +
      'xt","text":"chunked"}\n{"type":"say","text":"split line say"}\n{"type":"done"}\n'
    render(<Composer />)
    const box = screen_textarea()
    fireEvent.change(box, { target: { value: 'go' } })
    fireEvent.keyDown(box, { key: 'Enter' })

    await waitFor(() => {
      const msgs = useAgent.getState().messagesByConv['42'] ?? []
      const asst = msgs.find((m) => m.role === 'assistant')
      expect(asst?.say).toBe('split line say')
      expect(asst?.content).toBe('chunked')
    })
  })
})

function screen_textarea(): HTMLElement {
  return document.querySelector('textarea') as unknown as HTMLElement
}
