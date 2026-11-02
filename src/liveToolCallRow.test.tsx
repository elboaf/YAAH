// Regression: the live stream handler must create the ToolCall row on
// tool_start. b1f4629 (ponytail #539, 2026-10-28) accidentally dropped the
// startToolCall() call from the tool_start branch: every auto-approved tool
// result then arrived with no matching call row, finishToolCall matched
// nothing, and the result was silently dropped — live chips, results, and
// the #401 inline generate_image artwork all vanished mid-run (only the
// model's plain-text narration of "saved to ai-images/…" remained). Reload
// from history still worked, which is why the #401 DOM tests kept passing.
//
// Drives the REAL NDJSON stream (sayNdjsonSeam pattern) and asserts the
// finished generate_image call renders inline in the live buffer.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { useAgent } from './store'
import { Composer, MessageView } from './components'

const { getConfig, createConversation, listConversations, getMessages, getAgentTape } =
  vi.hoisted(() => ({
    getConfig: vi.fn(),
    createConversation: vi.fn(),
    listConversations: vi.fn(),
    getMessages: vi.fn(),
    getAgentTape: vi.fn(),
  }))

vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  getConfig,
  createConversation,
  listConversations,
  getMessages,
  getAgentTape,
}))

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

const genResult = {
  images: ['generate_image/regression-test.png'],
  path: 'ai-images/regression-test.png',
  saved_to: 'ai-images/regression-test.png',
  display: 'inline',
  width: 1024,
  height: 1024,
}

describe('live tool_start creates the call row (b1f4629 regression)', () => {
  let fetchMock: ReturnType<typeof vi.spyOn>
  let currentScript = ''

  beforeEach(() => {
    cleanup()
    vi.restoreAllMocks()
    getConfig.mockResolvedValue({})
    createConversation.mockResolvedValue({ id: 77 })
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

  it('a generate_image result lands on a live call and renders inline', async () => {
    currentScript =
      '{"type":"text","text":"Rendering your image."}\n' +
      '{"type":"tool_start","name":"generate_image","call_id":"call_gen1","args":{"prompt":"test card"}}\n' +
      `{"type":"tool_result","name":"generate_image","call_id":"call_gen1","result":${JSON.stringify(genResult)}}\n` +
      '{"type":"text","text":"Here it is."}\n' +
      '{"type":"done"}\n'
    render(<Composer />)
    const box = document.querySelector('textarea') as unknown as HTMLElement
    fireEvent.change(box, { target: { value: 'make an image' } })
    fireEvent.keyDown(box, { key: 'Enter' })

    // The result must attach to the live call row (finishToolCall matches
    // by call id — no started row, no result).
    await waitFor(() => {
      const msgs = useAgent.getState().messagesByConv['77'] ?? []
      const asst = msgs.find((m) => m.role === 'assistant')
      const tc = asst?.toolCalls?.find((t) => t.id === 'call_gen1')
      expect(tc?.result).toBeTruthy()
    })

    // #401 contract: the finished inline call renders the artwork in the
    // live buffer, not just after a history reload.
    const msgs = useAgent.getState().messagesByConv['77'] ?? []
    const asst = msgs.find((m) => m.role === 'assistant')!
    const { container } = render(<MessageView msg={asst} />)
    const img = container.querySelector('img[alt="generated image"]')
    expect(img).toBeTruthy()
    expect(img!.getAttribute('src')).toContain(
      '/api/images/generate_image/regression-test.png',
    )
    void fetchMock
  })

  it('renders a FAILED spawn_agent as a tool row, not an answered question', async () => {
    // Backend errors a spawn before emitting sub_agent_spawned (unknown
    // agent type, empty prompt): the call row never grows a subAgent, and
    // the message-level anchor branch used to fall through to
    // QuestionAnchor — rendering the error as an "answered question" card.
    currentScript =
      '{"type":"tool_start","name":"spawn_agent","call_id":"call_sp1","args":{"agent_type":"bogus","prompt":""}}\n' +
      '{"type":"tool_result","name":"spawn_agent","call_id":"call_sp1","result":{"error":"unknown agent type: bogus"}}\n' +
      '{"type":"done"}\n'
    render(<Composer />)
    const box = document.querySelector('textarea') as unknown as HTMLElement
    fireEvent.change(box, { target: { value: 'delegate nothing' } })
    fireEvent.keyDown(box, { key: 'Enter' })

    await waitFor(() => {
      const msgs = useAgent.getState().messagesByConv['77'] ?? []
      const asst = msgs.find((m) => m.role === 'assistant')
      const tc = asst?.toolCalls?.find((t) => t.id === 'call_sp1')
      expect(tc?.result).toBeTruthy()
      expect((tc!.result as { error?: string }).error).toContain('bogus')
    })

    const msgs = useAgent.getState().messagesByConv['77'] ?? []
    const asst = msgs.find((m) => m.role === 'assistant')!
    const { container } = render(<MessageView msg={asst} />)
    // The error text must be reachable via the audit expander, and no
    // question card may render for a spawn call.
    const expander = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('spawn_agent'),
    )
    expect(expander).toBeTruthy()
    fireEvent.click(expander!)
    expect(container.textContent).toContain('unknown agent type')
    expect(container.textContent).not.toContain('✓ answered')
    void fetchMock
  })
})
