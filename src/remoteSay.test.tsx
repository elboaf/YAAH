// #226 slice C: the remote device-chat composer ignored `say` events
// entirely, so briefings never landed on remote transcript messages —
// MessageView's say-line (and the say-only fallback) could never fire
// there. Pins the capture: a {"type":"say","text":...} event from the
// remote stream reaches the transcript message.
//
// Run: npx vitest run src/sayWire.test.tsx -t remote
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { cleanup, render, fireEvent, screen } from '@testing-library/react'
import { RemoteTranscriptDialog } from './components'
import { useAgent } from './store'
import { useRemoteConversations } from './remoteConversationStore'

const { getRemoteDeviceMessages, streamRemoteTurn } = vi.hoisted(() => ({
  getRemoteDeviceMessages: vi.fn(),
  streamRemoteTurn: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  getRemoteDeviceMessages,
  streamRemoteTurn,
}))

const row = (content: string) => ({
  id: 1,
  role: 'user',
  content,
  images: [],
  tool_call_id: null,
  tool_calls: null,
  sub_agent_transcript: null,
})

describe('remote composer captures say briefings (#226)', () => {
  beforeEach(() => {
    useAgent.setState({
      conversationId: 7,
      messagesByConv: {},
      statusByConv: {},
      pendingQuestions: {},
    })
    useRemoteConversations.setState({ transcripts: {} })
  })

  afterEach(() => {
    cleanup()
    getRemoteDeviceMessages.mockReset()
    streamRemoteTurn.mockReset()
    useAgent.setState({ conversationId: 7, messagesByConv: {} })
    useRemoteConversations.setState({ transcripts: {} })
  })

  it('lands the briefing on the transcript message', async () => {
    getRemoteDeviceMessages.mockResolvedValue([row('earlier message')])
    streamRemoteTurn.mockImplementation(async (_h: unknown, _c: unknown, _m: unknown, _w: unknown, onEvent: (ev: { type: string; text?: string }) => void) => {
      onEvent({ type: 'text', text: 'Visible remote answer.' })
      onEvent({ type: 'say', text: 'Remote briefing line.' })
      onEvent({ type: 'done' })
    })

    render(
      <RemoteTranscriptDialog hostId="host-a" conversationId="7" title="A chat" deviceName="A" online onClose={() => {}} />,
    )
    await screen.findByText('earlier message')

    const input = screen.getByLabelText('Message this device chat')
    fireEvent.change(input, { target: { value: 'run the tests' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await screen.findByText('Visible remote answer.')
    const key = 'remote:host-a:7'
    const msgs = useAgent.getState().messagesByConv[key] ?? []
    const asst = msgs.filter((m) => m.role === 'assistant').pop()
    expect(asst?.say).toBe('Remote briefing line.')
    // The transcript cache the dialog renders from carries it too.
    expect(
      useRemoteConversations.getState().getTranscript('host-a', '7')?.filter((m) => m.role === 'assistant').pop()?.say,
    ).toBe('Remote briefing line.')
  })
})
