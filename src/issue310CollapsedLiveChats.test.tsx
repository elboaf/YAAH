// Issue #310: a collapsed workspace group must not hide chats with live run
// state — working (streaming turn, or a scheduled-agent run in flight),
// blocked on the user (question / tool approval / plan approval), or
// finished-but-unacknowledged (ok / error). Such chats stay visible under the
// collapsed header, rendered by the same row renderer as expanded groups;
// chats with no live state stay hidden. State is derived from the store on
// every render, so a chat that gets blocked while collapsed appears without
// any user action.
// Run: npx vitest run src/issue310CollapsedLiveChats.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

const { listConversations, listWorkspaces } = vi.hoisted(() => ({
  listConversations: vi.fn(),
  listWorkspaces: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    listConversations,
    listWorkspaces,
  }
})

import { ConversationList } from './components'
import { useAgent } from './store'
import type { ConversationRow, WorkspaceRow } from './api'

const WS_PATH = 'C:/ws/demo'
// The localStorage key records the collapsed state per workspace path.
const expandKey = `yaah.group.expanded.${WS_PATH}`

function wsRow(over: Partial<WorkspaceRow> = {}): WorkspaceRow {
  return {
    id: 1,
    path: WS_PATH,
    label: 'demo',
    last_opened_at: null,
    exists: true,
    conversation_count: 2,
    owner_id: null,
    ...over,
  }
}

function conv(id: number, over: Partial<ConversationRow> = {}): ConversationRow {
  return {
    id,
    title: `chat ${id}`,
    workspace: WS_PATH,
    created_at: '2026-01-01 00:00:00',
    updated_at: `2026-01-01 00:0${id % 10}:00`,
    ...over,
  }
}

async function mountCollapsed(convs: ConversationRow[], workspaces: WorkspaceRow[] = [wsRow()]) {
  listConversations.mockResolvedValue(convs)
  listWorkspaces.mockResolvedValue(workspaces)
  render(<ConversationList addingDevice={false} setAddingDevice={() => {}} />)
  // The group header proves the fetched rows have been grouped; row content
  // lands a tick later (two independent promises).
  await screen.findByText('demo')
}

beforeEach(() => {
  localStorage.clear()
  listConversations.mockResolvedValue([])
  listWorkspaces.mockResolvedValue([])
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
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

describe('collapsed groups keep live chats visible (#310)', () => {
  it('a streaming chat stays visible under its collapsed header; idle chats stay hidden', async () => {
    localStorage.setItem(expandKey, '0')
    useAgent.setState({ statusByConv: { '11': 'thinking' } })
    await mountCollapsed([conv(11), conv(12)])

    expect(await screen.findByText('chat 11')).toBeTruthy()
    expect(screen.queryByText('chat 12')).toBeNull()
  })

  it('a chat blocked while collapsed appears without user action (orange bar)', async () => {
    localStorage.setItem(expandKey, '0')
    await mountCollapsed([conv(21), conv(22)])

    // Nothing is live at mount: no rows at all.
    expect(screen.queryByText('chat 21')).toBeNull()

    // The run parks on a question while the group is collapsed — the row
    // must appear on the next render, because the state is store-derived.
    act(() => {
      useAgent.setState({
        pendingQuestions: {
          // applyPending keys pending gates by their convKey (the chat id).
          '21': { callId: 'q1', question: 'Which way?', options: [], convKey: '21' },
        },
      })
    })
    expect(await screen.findByText('chat 21')).toBeTruthy()
    expect(document.querySelector('.run-bar-orange')).not.toBeNull()
  })

  it('finished-but-unacknowledged runs stay visible (green bar, red pill)', async () => {
    localStorage.setItem(expandKey, '0')
    useAgent.setState({ finishedByConv: { '31': 'ok', '32': 'error' } })
    await mountCollapsed([conv(31), conv(32), conv(33)])

    expect(await screen.findByText('chat 31')).toBeTruthy()
    expect(screen.queryByText('chat 32')).toBeTruthy()
    expect(screen.queryByText('chat 33')).toBeNull()
    expect(document.querySelector('.run-bar-green')).not.toBeNull()
    expect(document.querySelector('.run-bar-red')).not.toBeNull()
  })

  it('a scheduled-agent run in flight keeps the agent row visible while collapsed', async () => {
    localStorage.setItem(expandKey, '0')
    useAgent.setState({ agents: [{ ...agentRow(), running: true }] })
    await mountCollapsed([agentConv(), conv(12)])

    expect(await screen.findByText('nightly agent')).toBeTruthy()
    expect(screen.queryByText('chat 12')).toBeNull()
  })

  it('row precedence matches the expanded row: blocked beats working dots', async () => {
    localStorage.setItem(expandKey, '0')
    useAgent.setState({
      statusByConv: { '41': 'running-tool' },
      pendingApprovals: {
        // Keyed by convKey (the chat id), not the call id.
        '41': { callId: 'a1', tool: 'bash', args: {}, convKey: '41' },
      },
    })
    await mountCollapsed([conv(41)])

    await screen.findByText('chat 41')
    expect(document.querySelector('.run-bar-orange')).not.toBeNull()
    expect(document.querySelector('.run-dots')).toBeNull()
  })

  it('the header keeps its count badge and still toggles on click', async () => {
    localStorage.setItem(expandKey, '0')
    useAgent.setState({ statusByConv: { '51': 'thinking' } })
    await mountCollapsed([conv(51), conv(52)])

    await screen.findByText('chat 51')
    expect(screen.getByTitle('2 conversations in this workspace')).toBeTruthy()

    // Expanding shows everything, as today.
    fireEvent.click(screen.getByText('demo'))
    expect(await screen.findByText('chat 52')).toBeTruthy()
  })
})

function agentRow() {
  return {
    id: 'agent-1',
    workspace: WS_PATH,
    name: 'nightly',
    prompt: 'do the thing',
    schedule_type: 'daily' as const,
    schedule_spec: { time: '09:00' },
    schedule_text: 'daily at 09:00',
    approval_policy: 'autonomous' as const,
    landing_mode: 'fixed' as const,
    landing_branch: 'run/nightly',
    say_mode: 'arrival' as const,
    model: '',
    effort: '',
    memory_enabled: false,
    allow_ask_user: false,
    retention: 5,
    notify_on_success: false,
    enabled: true,
    running: false,
    conversation_id: 91,
    next_fire_at: '',
    last_fired_at: '',
    last_finished_at: '',
    last_status: '',
    instructions: [],
    chat_title: 'nightly agent',
  }
}

function agentConv(): ConversationRow {
  return {
    id: 91,
    title: 'nightly agent',
    workspace: WS_PATH,
    created_at: '2026-01-01 00:00:00',
    updated_at: '2026-01-01 00:00:00',
    chat_type: 'agent',
  }
}
