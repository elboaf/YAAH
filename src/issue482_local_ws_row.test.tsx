// Issue #482 (owner repro): a locally-owned chat filed under a DEVICE's
// workspace shows the sidebar status slot - yellow dots while it streams,
// orange bar while parked on ask_user, green bar when it finishes. Its turn
// streams through the local loop into the same numeric-keyed maps every other
// row reads; the row just never rendered them before.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const { listRemoteDeviceConversations } = vi.hoisted(() => ({
  listRemoteDeviceConversations: vi.fn(),
}))
vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  listRemoteDeviceConversations,
}))
import { DeviceGroups } from './components'
import { useAgent } from './store'
import { nsWorkspace } from './remoteStore'
import type { RemoteDevice, WorkspaceRow } from './api'

const HOST = 'host-a'
const WS = nsWorkspace(HOST, 'C:/repo')
const device: RemoteDevice = { host_id: HOST, url: 'http://a', name: 'Host A', status: 'online', workspaces: [] }
const wsRow: WorkspaceRow = { id: 5, path: WS, label: 'repo', last_opened_at: null, exists: true, conversation_count: 1, owner_id: HOST, device_status: 'online' }
const conv = { id: 42, title: 'local on device ws', workspace: WS, updated_at: '2026-01-01T00:00:00' }

const resetStore = () =>
  useAgent.setState({ statusByConv: {}, finishedByConv: {}, pendingQuestions: {}, activeRemoteKey: null, conversationId: null, messagesByConv: {} })

beforeEach(() => {
  listRemoteDeviceConversations.mockResolvedValue({ status: 'online', conversations: [] })
  resetStore()
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  resetStore()
})

const renderRow = async () => {
  render(
    <DeviceGroups
      devices={[device]}
      workspaces={[wsRow]}
      conversations={[conv]}
      onChange={() => {}}
      onOpenConversation={() => {}}
      adding={false}
      setAdding={() => {}}
    />,
  )
  return await screen.findByTitle(/locally owned chat using this device's workspace/)
}

describe('issue #482: locally-owned chat on a device workspace shows sidebar signals', () => {
  it('yellow dots while the turn streams', async () => {
    useAgent.setState((s) => ({ statusByConv: { ...s.statusByConv, '42': 'thinking' } }))
    const row = await renderRow()
    expect(row.querySelector('.run-dots')).not.toBeNull()
  })

  it('orange bar while parked on a pending ask_user', async () => {
    useAgent.setState((s) => ({
      pendingQuestions: { ...s.pendingQuestions, '42': { callId: 'c1', question: 'q', options: [], convKey: '42' } },
    }))
    const row = await renderRow()
    expect(row.querySelector('.run-bar-orange')).not.toBeNull()
  })

  it('green bar when the run finished unacknowledged', async () => {
    useAgent.setState((s) => ({ finishedByConv: { ...s.finishedByConv, '42': 'ok' } }))
    const row = await renderRow()
    expect(row.querySelector('.run-bar-green')).not.toBeNull()
  })

  it('idle row renders no signal', async () => {
    const row = await renderRow()
    expect(row.querySelector('.run-dots')).toBeNull()
    expect(row.querySelector('.run-bar')).toBeNull()
  })
})
