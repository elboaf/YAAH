// Issue #308 follow-up: remote ask_user parity. The remote turn's ask_user
// call renders the answer card in the remote transcript dialog (answers go to
// the device-turn answer endpoint) and the sidebar row shows the blocked
// yellow bar while a question is parked.
// Run: npx vitest run src/issue308RemoteAskUser.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { getRemoteDeviceMessages, listRemoteDeviceConversations, streamRemoteTurn, submitRemoteAnswer } = vi.hoisted(() => ({
  getRemoteDeviceMessages: vi.fn(),
  listRemoteDeviceConversations: vi.fn(),
  streamRemoteTurn: vi.fn(),
  submitRemoteAnswer: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  getRemoteDeviceMessages,
  listRemoteDeviceConversations,
  streamRemoteTurn,
  submitRemoteAnswer,
}))

import { DeviceGroups, RemoteTranscriptDialog } from './components'
import { useAgent } from './store'
import { remoteConversationKey, useRemoteConversations } from './remoteConversationStore'
import type { RemoteDevice, WorkspaceRow } from './api'

const KEY = remoteConversationKey('host-a', '7')

const row = (content: string) => ({
  id: 1,
  role: 'user' as const,
  content,
  images: [],
  tool_call_id: null,
  tool_calls: null,
  sub_agent_transcript: null,
})

const device: RemoteDevice = { host_id: 'host-a', url: 'http://a', name: 'Host A', status: 'online', workspaces: [] }
const workspaces: WorkspaceRow[] = []

beforeEach(() => {
  getRemoteDeviceMessages.mockResolvedValue([row('earlier message')])
  listRemoteDeviceConversations.mockResolvedValue({ status: 'online', conversations: [] })
  useAgent.setState({ statusByConv: {}, finishedByConv: {}, activeRemoteKey: null, conversationId: null, messagesByConv: {}, pendingQuestions: {} })
  useRemoteConversations.setState({ transcripts: {} })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  useAgent.setState({ statusByConv: {}, finishedByConv: {}, activeRemoteKey: null, conversationId: null, messagesByConv: {}, pendingQuestions: {} })
  useRemoteConversations.setState({ transcripts: {} })
})

describe('remote ask_user (#308 follow-up)', () => {
  it('the ask tool_start renders the card and an answer hits the remote endpoint', async () => {
    let deliverEvent: ((ev: { type: string; name?: string; args?: unknown; call_id?: string }) => void) | null = null
    streamRemoteTurn.mockImplementation((_h, _c, _m, _w, onEvent) => {
      deliverEvent = onEvent
      return new Promise(() => { /* stream stays open while parked */ })
    })
    submitRemoteAnswer.mockResolvedValue({ ok: true })

    render(<RemoteTranscriptDialog hostId="host-a" conversationId="7" title="Remote chat" online deviceName="Host A" onClose={() => {}} />)
    await screen.findByText('earlier message')

    await act(async () => {
      fireEvent.change(screen.getByLabelText('Message this device chat'), { target: { value: 'go' } })
    })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /send/i })) })
    await waitFor(() => expect(streamRemoteTurn).toHaveBeenCalled())

    await act(async () => {
      deliverEvent!({ type: 'tool_start', name: 'ask_user', call_id: 'c1', args: { question: 'which color?', options: [{ label: 'red' }, { label: 'blue' }] } })
    })
    expect(await screen.findByText('which color?')).toBeTruthy()

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'blue' })) })
    await waitFor(() => expect(submitRemoteAnswer).toHaveBeenCalledWith('host-a', '7', 'c1', 'blue'))
    // The card clears on answer.
    await waitFor(() => expect(useAgent.getState().pendingQuestions[KEY]).toBeUndefined())
  })

  it('the sidebar row shows the blocked yellow bar while a question is parked', async () => {
    listRemoteDeviceConversations.mockResolvedValue({
      status: 'online',
      conversations: [{ host_id: 'host-a', conversation_id: '7', title: 'device chat', workspace: null, updated_at: '2025-01-01', revision: '', sync_status: 'synced' }],
    })
    useAgent.setState({ pendingQuestions: { [KEY]: { callId: 'c1', question: '?', options: [], convKey: KEY } } })
    render(<DeviceGroups devices={[device]} workspaces={workspaces} conversations={[]} onChange={() => {}} onOpenConversation={() => {}} adding={false} setAdding={() => {}} />)
    await screen.findByText('device chat')
    const bar = document.querySelector('.run-bar-orange')
    expect(bar).toBeTruthy()
    expect(bar?.getAttribute('title')).toMatch(/question/i)
  })

  it('a mid-stream failure clears the parked question (it can never be answered)', async () => {
    streamRemoteTurn.mockImplementation((_h, _c, _m, _w, onEvent) => {
      return new Promise((_resolve, reject) => {
        onEvent({ type: 'tool_start', name: 'ask_user', call_id: 'c1', args: { question: 'which?' } })
        setTimeout(() => reject(new Error('stream died')), 20)
      })
    })

    render(<RemoteTranscriptDialog hostId="host-a" conversationId="7" title="Remote chat" online deviceName="Host A" onClose={() => {}} />)
    await screen.findByText('earlier message')
    await act(async () => {
      fireEvent.change(screen.getByLabelText('Message this device chat'), { target: { value: 'go' } })
    })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /send/i })) })
    await waitFor(() => expect(screen.getByText('which?')).toBeTruthy())
    await waitFor(() => expect(useAgent.getState().statusByConv[KEY]).toBe('error'))
    await waitFor(() => expect(useAgent.getState().pendingQuestions[KEY]).toBeUndefined())
  })
})