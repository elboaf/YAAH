// Issue #382: chats created in the Default workspace were saved with
// workspace='' while the sidebar groups Default by NULL matching — the rows
// matched no group and vanished. These tests pin the two frontend layers:
// the wire contract ('' must travel as null) and the sidebar grouping
// (legacy '' rows must still render under Default).
// Run: npx vitest run src/issue382DefaultWorkspaceEmptyString.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const { listConversations, listWorkspaces, fetchMock } = vi.hoisted(() => ({
  listConversations: vi.fn(),
  listWorkspaces: vi.fn(),
  fetchMock: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    listConversations,
    listWorkspaces,
  }
})

import { createConversation, moveConversation, type ConversationRow, type WorkspaceRow } from './api'
import { ConversationList } from './components'
import { useAgent } from './store'

const jsonRes = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })

function wsRow(over: Partial<WorkspaceRow> = {}): WorkspaceRow {
  return {
    id: 1,
    path: null, // the Default registry row
    label: 'Default',
    last_opened_at: null,
    exists: true,
    conversation_count: 1,
    owner_id: null,
    ...over,
  }
}

function conv(id: number, over: Partial<ConversationRow> = {}): ConversationRow {
  return {
    id,
    title: `chat ${id}`,
    workspace: null,
    created_at: '2026-01-01 00:00:00',
    updated_at: `2026-01-01 00:0${id % 10}:00`,
    ...over,
  }
}

beforeEach(() => {
  localStorage.clear()
  listConversations.mockResolvedValue([])
  listWorkspaces.mockResolvedValue([])
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
  useAgent.setState({
    statusByConv: {},
    pendingQuestions: {},
    pendingApprovals: {},
    pendingPlanApprovals: {},
    finishedByConv: {},
    agents: [],
  })
})

describe('#382 wire contract', () => {
  it('createConversation sends Default as null, never an empty string', async () => {
    fetchMock.mockResolvedValue(jsonRes({ id: 1 }))
    vi.stubGlobal('fetch', fetchMock)
    await createConversation('t', '')
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).workspace).toBeNull()
  })

  it('moveConversation sends Default as null, never an empty string', async () => {
    fetchMock.mockResolvedValue(jsonRes({ ok: true, workspace: null, target_id: null }))
    vi.stubGlobal('fetch', fetchMock)
    await moveConversation(7, '')
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).workspace).toBeNull()
  })
})

describe('#382 sidebar grouping', () => {
  it('a legacy row stored with workspace="" still renders under Default', async () => {
    // Poisoned row exactly as the live DB still holds it (chats 783/786).
    listConversations.mockResolvedValue([conv(783, { workspace: '' })])
    listWorkspaces.mockResolvedValue([wsRow()])
    render(<ConversationList addingDevice={false} setAddingDevice={() => {}} />)
    expect(await screen.findByText('chat 783')).toBeTruthy()
  })

  it('a healthy NULL row keeps rendering under Default (no regression)', async () => {
    listConversations.mockResolvedValue([conv(1)])
    listWorkspaces.mockResolvedValue([wsRow()])
    render(<ConversationList addingDevice={false} setAddingDevice={() => {}} />)
    expect(await screen.findByText('chat 1')).toBeTruthy()
  })
})
