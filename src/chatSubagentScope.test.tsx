import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

const {
  getConfig,
  getConversation,
  updateConversation,
  updateAgentModelEffort,
  listAvailableModels,
} = vi.hoisted(() => ({
  getConfig: vi.fn(),
  getConversation: vi.fn(),
  updateConversation: vi.fn(),
  updateAgentModelEffort: vi.fn(),
  listAvailableModels: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    getConfig,
    getConversation,
    updateConversation,
    updateAgentModelEffort,
    listAvailableModels,
  }
})

import { ChatScopePickers, SUBAGENT_AUTO_LABEL } from './components'
import { useAgent } from './store'

const byProvider = {
  providers: {
    groq: { models: ['llama-3.3-70b'], error: null },
    openai: { models: ['gpt-5.2'], error: null },
  },
}

/** The header renders model/effort/sub-agent selects; the model select
 *  repeats the same option labels, so every query scopes to one picker. */
const findSubagentPicker = () =>
  screen.findByRole('combobox', { name: 'Chat sub-agent model' })

describe('chat sub-agent scope picker (#383)', () => {
  beforeEach(() => {
    getConfig.mockResolvedValue({ model: 'groq::llama-3.3-70b', reasoning_effort: '' })
    listAvailableModels.mockResolvedValue(byProvider)
    updateConversation.mockResolvedValue({ ok: true })
    updateAgentModelEffort.mockResolvedValue({ id: 'a1' })
    useAgent.setState({
      conversationId: null,
      agents: [],
      globalModel: 'groq::llama-3.3-70b',
      globalEffort: '',
      draftScope: { model: 'groq::llama-3.3-70b', effort: '', subagent_model: '' },
    })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    useAgent.setState({ conversationId: null, agents: [], draftScope: null })
  })

  it('drafts show "sub-agent: auto" plus provider-grouped discovered models', async () => {
    render(<ChatScopePickers />)
    const picker = await findSubagentPicker()
    expect(picker).toHaveValue('')
    await waitFor(() => {
      expect(within(picker).getByRole('option', { name: 'gpt-5.2' })).toBeInTheDocument()
    })
    expect(within(picker).getByRole('option', { name: SUBAGENT_AUTO_LABEL })).toBeInTheDocument()
    // Grouped by provider, like the primary selector.
    expect(within(picker).getByRole('group', { name: 'openai' })).toBeInTheDocument()
  })

  it('a draft edit lands in draftScope and is pinned at first send', async () => {
    render(<ChatScopePickers />)
    const picker = await findSubagentPicker()
    await waitFor(() =>
      expect(within(picker).getByRole('option', { name: 'gpt-5.2' })).toBeInTheDocument(),
    )

    fireEvent.change(picker, { target: { value: 'openai::gpt-5.2' } })

    expect(useAgent.getState().draftScope?.subagent_model).toBe('openai::gpt-5.2')
    expect(picker).toHaveValue('openai::gpt-5.2')
    // A draft has no row to PATCH.
    expect(updateConversation).not.toHaveBeenCalled()
  })

  it('a saved chat PATCHes its row; empty value = sub-agent: auto', async () => {
    getConversation.mockResolvedValue({
      id: 7,
      model: 'groq::llama-3.3-70b',
      effort: '',
      subagent_model: 'openai::gpt-5.2',
    })
    useAgent.setState({ conversationId: 7 })
    render(<ChatScopePickers />)
    const picker = await findSubagentPicker()
    await waitFor(() => expect(picker).toHaveValue('openai::gpt-5.2'))

    fireEvent.change(picker, { target: { value: '' } })

    // '' (auto) is a legal write — the deliberate no-pin state.
    await waitFor(() =>
      expect(updateConversation).toHaveBeenCalledWith(7, { subagent_model: '' }),
    )
  })

  it('an agent-pinned chat writes through to the owning agent', async () => {
    getConversation.mockResolvedValue({
      id: 91,
      model: '',
      effort: '',
      subagent_model: '',
    })
    useAgent.setState({
      conversationId: 91,
      agents: [
        {
          id: 'agent-1',
          workspace: '',
          name: 'nightly',
          prompt: 'p',
          schedule_type: 'daily',
          schedule_spec: { time: '09:00' },
          schedule_text: 'daily at 09:00',
          approval_policy: 'autonomous',
          say_mode: 'arrival',
          model: '',
          effort: '',
          subagent_model: '',
          memory_enabled: false,
          allow_ask_user: false,
          retention: 0,
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
        },
      ],
    })
    render(<ChatScopePickers />)
    const picker = await findSubagentPicker()
    await waitFor(() => expect(picker).toHaveValue(''))

    fireEvent.change(picker, { target: { value: 'openai::gpt-5.2' } })

    await waitFor(() =>
      expect(updateAgentModelEffort).toHaveBeenCalledWith('agent-1', '', '', 'openai::gpt-5.2'),
    )
  })
})
