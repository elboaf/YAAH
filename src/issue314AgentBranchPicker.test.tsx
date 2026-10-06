import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { addAgent, updateAgent, getWorkspaceGitBranches } = vi.hoisted(() => ({
  addAgent: vi.fn(),
  updateAgent: vi.fn(),
  getWorkspaceGitBranches: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    addAgent,
    updateAgent,
    getWorkspaceGitBranches,
  }
})

import { AgentForm } from './components'

const ws = 'C:/repos/project'

const branchesPayload = {
  branch: 'master',
  branches: ['master', 'nightly', 'scratch'],
}

beforeEach(() => {
  getWorkspaceGitBranches.mockResolvedValue(branchesPayload)
  addAgent.mockResolvedValue({
    id: 'a1',
    name: 'test agent',
    conversation_id: 11,
  })
  updateAgent.mockResolvedValue({
    id: 'a1',
    name: 'test agent',
    conversation_id: 11,
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const baseAgent = {
  id: 'a1',
  workspace: ws,
  name: 'test agent',
  prompt: 'do the thing',
  schedule_type: 'interval' as const,
  schedule_spec: { minutes: 30 },
  schedule_text: 'every 30 min',
  approval_policy: 'sandbox-only' as const,
  landing_mode: 'off' as const,
  landing_branch: '',
  say_mode: 'arrival' as const,
  model: '',
  effort: '',
  memory_enabled: true,
  allow_ask_user: false,
  retention: 0,
  notify_on_success: false,
  enabled: true,
  conversation_id: 11,
  next_fire_at: '',
  last_fired_at: '',
  last_finished_at: '',
  last_status: 'ok',
  running: false,
  instructions: [],
  chat_title: 'test agent',
  chat_selected_branch: 'master',
  chat_branch_pin_origin: 'inherited' as const,
}

describe('issue #314: agent form branch picker', () => {
  it('creates with an explicit branch pick', async () => {
    render(<AgentForm agent={null} wsPath={ws} onDone={() => {}} onCancel={() => {}} />)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'test agent' } })
    fireEvent.change(screen.getByLabelText('Prompt — what the agent does on every run'), {
      target: { value: 'do the thing' },
    })
    // The branch list arrives async — wait for the option before picking.
    await screen.findByRole('option', { name: 'nightly' })
    fireEvent.change(screen.getByLabelText('Initial branch'), {
      target: { value: 'nightly' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Create agent' }))
    await waitFor(() => expect(addAgent).toHaveBeenCalled())
    expect(addAgent.mock.calls[0][0].selected_branch).toBe('nightly')
  })

  it('creates with no selected_branch on the inherit default', async () => {
    render(<AgentForm agent={null} wsPath={ws} onDone={() => {}} onCancel={() => {}} />)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'x' } })
    fireEvent.change(screen.getByLabelText('Prompt — what the agent does on every run'), {
      target: { value: 'p' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Create agent' }))
    await waitFor(() => expect(addAgent).toHaveBeenCalled())
    const body = addAgent.mock.calls[0][0]
    expect(body.selected_branch === undefined || body.selected_branch === '').toBe(true)
  })

  it('editing with an explicit pick sends it; the inherit default sends none', async () => {
    render(
      <AgentForm
        agent={baseAgent}
        wsPath={ws}
        onDone={() => {}}
        onCancel={() => {}}
      />,
    )
    // The branch list arrives async — wait for the option before picking.
    await screen.findByRole('option', { name: 'nightly' })
    fireEvent.change(screen.getByLabelText('Initial branch'), {
      target: { value: 'nightly' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save agent' }))
    await waitFor(() => expect(updateAgent).toHaveBeenCalled())
    expect(updateAgent.mock.calls[0][0]).toBe('a1')
    expect(updateAgent.mock.calls[0][1].selected_branch).toBe('nightly')
  })

  it('picker lists the workspace branches with the inherit head', async () => {
    render(<AgentForm agent={null} wsPath={ws} onDone={() => {}} onCancel={() => {}} />)
    const select = await screen.findByLabelText('Initial branch')
    await waitFor(() => expect(getWorkspaceGitBranches).toHaveBeenCalledWith(ws))
    const options = Array.from(select.querySelectorAll('option')).map(
      (o) => o.getAttribute('value') ?? '',
    )
    expect(options[0]).toBe('')
    expect(options).toContain('master')
    expect(options).toContain('nightly')
    expect(options).toContain('scratch')
  })

  it('picker is disabled for a workspace with no branch list', async () => {
    getWorkspaceGitBranches.mockResolvedValue({ branch: null, branches: [] })
    render(<AgentForm agent={null} wsPath={ws} onDone={() => {}} onCancel={() => {}} />)
    const select = await screen.findByLabelText('Initial branch')
    await waitFor(() => expect(select).toBeDisabled())
  })

  it('off mode names the pinned chat it lands on (edit)', async () => {
    render(
      <AgentForm
        agent={{ ...baseAgent, chat_selected_branch: 'nightly', chat_branch_pin_origin: 'explicit' }}
        wsPath={ws}
        onDone={() => {}}
        onCancel={() => {}}
      />,
    )
    expect(screen.getByText(/lands on nightly \(explicit pick\)/)).toBeTruthy()
  })

  it('create mode says the inherit target, not a phantom chat', async () => {
    render(<AgentForm agent={null} wsPath={ws} onDone={() => {}} onCancel={() => {}} />)
    expect(screen.getByText(/will inherit the workspace's current branch at creation/)).toBeTruthy()
    expect(screen.queryByText(/lands on/)).toBeNull()
  })

  it('an untouched edit save sends no branch (never re-origins the pin)', async () => {
    /** The picker must rest on the inherit default on edit — initializing
     *  from the chat's current branch would silently stamp an inherited
     *  pin as explicit on every save. The current branch shows in the
     *  caption instead. */
    render(
      <AgentForm
        agent={{ ...baseAgent, chat_selected_branch: 'master', chat_branch_pin_origin: 'inherited' }}
        wsPath={ws}
        onDone={() => {}}
        onCancel={() => {}}
      />,
    )
    await screen.findByRole('option', { name: 'master' })
    fireEvent.click(screen.getByRole('button', { name: 'Save agent' }))
    await waitFor(() => expect(updateAgent).toHaveBeenCalled())
    expect(updateAgent.mock.calls[0][1].selected_branch).toBeUndefined()
  })

  it('off option reads as following the chat selector', async () => {
    render(<AgentForm agent={null} wsPath={ws} onDone={() => {}} onCancel={() => {}} />)
    const opt = screen.getByRole('option', { name: /follows the chat's branch selector/ }) as HTMLOptionElement
    expect(opt.value).toBe('off')
  })
})
