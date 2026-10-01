// Regression (#226): the say wire contract. Since #66 the backend emits
// `{"type": "say", "text": "<briefing>"}` on the agent stream, but the
// Composer's stream handler read `ev.say` — a field that never existed on
// the wire — so every briefing was captured as '' and (a) the #207
// "show <say> emissions in chat" line could never render and (b) live TTS
// always fell back to the heuristic first/last-sentence read. This test
// drives the REAL Composer handler against the REAL wire shape (mocked
// transport, genuine streamAgentTurn-free dispatch via the component) and
// pins the contract: the briefing lands on the message, the tag stays out
// of the chat body.
//
// Run: npx vitest run src/sayWire.test.tsx
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent, screen, waitFor } from '@testing-library/react'
import { useAgent } from './store'
import { Composer } from './components'

const { streamAgentTurn, getConfig, createConversation } = vi.hoisted(() => ({
  streamAgentTurn: vi.fn(),
  getConfig: vi.fn(),
  createConversation: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  streamAgentTurn,
  getConfig,
  createConversation,
}))

describe('say wire contract (#226)', () => {
  beforeEach(() => {
    streamAgentTurn.mockReset()
    getConfig.mockResolvedValue({})
    createConversation.mockResolvedValue({ id: 42 })
    useAgent.setState({
      conversationId: null,
      statusByConv: {},
      messagesByConv: {},
      pendingQuestions: {},
      pendingApprovals: {},
      pendingPlanApprovals: {},
    })
  })

  it('captures the briefing from {"type":"say","text":...} onto the message', async () => {
    streamAgentTurn.mockImplementation(
      async (_cid: unknown, _msg: unknown, _ws: unknown, onEvent: (ev: { type: string; text?: string }) => void) => {
        onEvent({ type: 'text', text: 'Visible answer.' })
        onEvent({ type: 'say', text: 'Fixed the wire, tests next.' })
        onEvent({ type: 'done' })
      },
    )
    render(<Composer />)

    const box = screen.getByLabelText('Message the agent') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'do the thing' } })
    fireEvent.keyDown(box, { key: 'Enter' })

    // The draft is adopted as conversation 42 before the stream starts; the
    // handler captured the re-homed key, so the live buffer lives there.
    // The send path is async (createConversation resolves first), so wait
    // for the stream to have run.
    await waitFor(() => {
      const msgs = useAgent.getState().messagesByConv['42'] ?? []
      const asst = msgs.find((m) => m.role === 'assistant')
      expect(asst?.say).toBe('Fixed the wire, tests next.')
    })
    const asst = (useAgent.getState().messagesByConv['42'] ?? []).find(
      (m) => m.role === 'assistant',
    )
    expect(asst?.content).toBe('Visible answer.')
  })
})
