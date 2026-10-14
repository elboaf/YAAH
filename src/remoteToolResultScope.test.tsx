// Issue #401 review follow-up (CodeRabbit): the remote-device live stream
// stored tool results verbatim, so image rels inside ev.result stayed
// LOCAL-scoped and every streamed remote image (generate_image, view_image,
// MCP attachments) pointed at the viewer's own /api/images endpoint instead
// of the owning device's. History load scopes via scopeRemoteMedia; the
// live path must too.
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

describe('streamed remote tool results scope media to the device (#401)', () => {
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

  it('an image result arriving over the stream carries the remote ref', async () => {
    getRemoteDeviceMessages.mockResolvedValue([row('earlier message')])
    streamRemoteTurn.mockImplementation(async (_h: unknown, _c: unknown, _m: unknown, _w: unknown, onEvent: (ev: Record<string, unknown>) => void) => {
      onEvent({ type: 'tool_start', name: 'generate_image', call_id: 'call_r1', args: { prompt: 'a gecko' } })
      onEvent({
        type: 'tool_result',
        name: 'generate_image',
        call_id: 'call_r1',
        result: { images: ['generate_image/abc.png'], saved_to: 'ai-images/abc.png', display: 'inline' },
      })
      onEvent({ type: 'done' })
    })

    render(
      <RemoteTranscriptDialog hostId="host-a" conversationId="7" title="A chat" deviceName="A" online onClose={() => {}} />,
    )
    await screen.findByText('earlier message')

    const input = screen.getByLabelText('Message this device chat')
    fireEvent.change(input, { target: { value: 'draw a gecko' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    // The inline ToolCallRow branch renders the artwork (unique alt); the
    // ticker chip's name text is not unique.
    await screen.findByAltText('generated image')
    const msgs = useAgent.getState().messagesByConv['remote:host-a:7'] ?? []
    const asst = msgs.filter((m) => m.role === 'assistant').pop()
    const call = asst?.toolCalls?.find((t) => t.id === 'call_r1')
    expect(call?.result).toBeTruthy()
    const images = (call!.result as { images?: string[] }).images ?? []
    expect(images[0]).toBe('remote-image:host-a:generate_image/abc.png')
  })
})
