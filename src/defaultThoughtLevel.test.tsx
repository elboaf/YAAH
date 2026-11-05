import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

const {
  getConfig,
  getConversation,
  updateConversation,
  updateConfig,
  setActiveModel,
  updateAgentModelEffort,
  listAvailableModels,
} = vi.hoisted(() => ({
  getConfig: vi.fn(),
  getConversation: vi.fn(),
  updateConversation: vi.fn(),
  updateConfig: vi.fn(),
  setActiveModel: vi.fn(),
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
    updateConfig,
    setActiveModel,
    updateAgentModelEffort,
    listAvailableModels,
  }
})

import { ChatScopePickers, modelListStale } from './components'
import { useAgent } from './store'

const byProvider = {
  providers: {
    openrouter: {
      models: ['reasoner'],
      model_info: [{ id: 'reasoner', reasoning_efforts: ['low', 'high', 'max'], supports_reasoning: true }],
    },
    groq: { models: ['llama-3.3-70b'], model_info: [] },
  },
}

const findModelPicker = () => screen.findByRole('combobox', { name: 'Chat model' })
const findEffortPicker = () => screen.findByRole('combobox', { name: 'Chat reasoning effort' })

describe('composer rail lockstep (#685): picks move the default', () => {
  beforeEach(() => {
    getConfig.mockResolvedValue({ model: 'openrouter::reasoner', reasoning_effort: 'high' })
    listAvailableModels.mockResolvedValue(byProvider)
    updateConversation.mockResolvedValue({ ok: true })
    updateAgentModelEffort.mockResolvedValue({ id: 'a1' })
    updateConfig.mockResolvedValue({ ok: true })
    setActiveModel.mockResolvedValue({ ok: true })
    getConversation.mockResolvedValue({ id: 42, model: 'openrouter::reasoner', effort: 'high', subagent_model: '' })
    useAgent.setState({
      conversationId: 42,
      agents: [],
      globalModel: 'openrouter::reasoner',
      globalEffort: 'high',
      draftScope: null,
    })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    vi.useRealTimers()
    useAgent.setState({ conversationId: null, agents: [], draftScope: null })
  })

  it('a pick in a normal chat rewrites the default (active model + effort guard)', async () => {
    render(<ChatScopePickers />)
    const picker = await findModelPicker()
    await waitFor(() => expect(picker).toHaveValue('openrouter::reasoner'))

    fireEvent.change(picker, { target: { value: 'groq::llama-3.3-70b' } })

    // Lockstep write #1: the chat row.
    await waitFor(() => expect(updateConversation).toHaveBeenCalledWith(42, { model: 'groq::llama-3.3-70b', effort: '' }))
    // Lockstep write #2: the global default (the old sidebar picker's job).
    await waitFor(() => expect(setActiveModel).toHaveBeenCalledWith('groq', 'llama-3.3-70b'))
    // The default effort resets when the new model doesn't advertise it.
    await waitFor(() => expect(updateConfig).toHaveBeenCalledWith({ reasoning_effort: '' }))
  })

  it('a pick in a fresh draft writes the default without a conversation write', async () => {
    useAgent.setState({ conversationId: null, draftScope: { model: 'openrouter::reasoner', effort: 'high', subagent_model: '' } })
    render(<ChatScopePickers />)
    const picker = await findModelPicker()
    await waitFor(() => expect(picker).toHaveValue('openrouter::reasoner'))

    fireEvent.change(picker, { target: { value: 'groq::llama-3.3-70b' } })

    await waitFor(() => expect(setActiveModel).toHaveBeenCalledWith('groq', 'llama-3.3-70b'))
    await waitFor(() => expect(useAgent.getState().draftScope?.model).toBe('groq::llama-3.3-70b'))
    expect(updateConversation).not.toHaveBeenCalled()
  })

  it('an effort pick in a normal chat moves the default effort too', async () => {
    render(<ChatScopePickers />)
    const effort = await findEffortPicker()
    await waitFor(() => expect(screen.getByRole('option', { name: 'Low' })).toBeInTheDocument())

    fireEvent.change(effort, { target: { value: 'low' } })

    await waitFor(() => expect(updateConversation).toHaveBeenCalledWith(42, { effort: 'low' }))
    await waitFor(() => expect(updateConfig).toHaveBeenCalledWith({ reasoning_effort: 'low' }))
  })

  it('an agent-chat pick writes through to the agent and never moves the default', async () => {
    useAgent.setState({
      agents: [{
        id: 'a1',
        workspace: '',
        name: 'nightly',
        prompt: 'p',
        schedule_type: 'daily',
        schedule_spec: { time: '09:00' },
        schedule_text: 'daily at 09:00',
        approval_policy: 'autonomous',
        say_mode: 'arrival',
        model: 'openrouter::reasoner',
        effort: 'high',
        subagent_model: '',
        memory_enabled: false,
        allow_ask_user: false,
        retention: 0,
        notify_on_success: false,
        enabled: true,
        running: false,
        conversation_id: 42,
        next_fire_at: '',
        last_fired_at: '',
        last_finished_at: '',
        last_status: '',
        instructions: [],
        chat_title: 'nightly agent',
      }],
    })
    render(<ChatScopePickers />)
    const picker = await findModelPicker()
    await waitFor(() => expect(picker).toHaveValue('openrouter::reasoner'))

    fireEvent.change(picker, { target: { value: 'groq::llama-3.3-70b' } })

    // The agent's own scope is updated (with the effort reset the new model forces).
    await waitFor(() => expect(updateAgentModelEffort).toHaveBeenCalledWith('a1', 'groq::llama-3.3-70b', ''))
    // Q5 exemption: no default write leaves an agent chat.
    expect(setActiveModel).not.toHaveBeenCalled()
    expect(updateConfig).not.toHaveBeenCalled()
    expect(updateConversation).not.toHaveBeenCalled()
  })
})

describe('hover-to-refresh TTL (#685)', () => {
  beforeEach(() => {
    getConfig.mockResolvedValue({ model: 'openrouter::reasoner', reasoning_effort: 'high' })
    listAvailableModels.mockResolvedValue(byProvider)
    updateConfig.mockResolvedValue({ ok: true })
    setActiveModel.mockResolvedValue({ ok: true })
    useAgent.setState({
      conversationId: null,
      agents: [],
      globalModel: 'openrouter::reasoner',
      globalEffort: 'high',
      draftScope: null,
    })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    vi.useRealTimers()
    useAgent.setState({ conversationId: null, agents: [], draftScope: null })
  })

  it('a fresh hover does not re-probe; a stale hover does', async () => {
    render(<ChatScopePickers />)
    const picker = await findModelPicker()
    await waitFor(() => expect(within(picker).getByRole('option', { name: 'llama-3.3-70b' })).toBeInTheDocument())
    const probesAfterMount = listAvailableModels.mock.calls.length
    expect(probesAfterMount).toBeGreaterThan(0)

    // Fresh hover inside the 60s window: the mount probe just stamped the
    // shared clock, so no new network call may fire.
    fireEvent.mouseEnter(picker)
    expect(listAvailableModels.mock.calls.length).toBe(probesAfterMount)

    // Once the clock passes the TTL, the same hover must re-probe.
    vi.useFakeTimers()
    vi.setSystemTime(Date.now() + 61_000)
    fireEvent.mouseEnter(picker)
    expect(listAvailableModels.mock.calls.length).toBeGreaterThan(probesAfterMount)
  })

  it('mouseEnter on the effort picker runs the same shared-clock stale-check', async () => {
    render(<ChatScopePickers />)
    const effort = await findEffortPicker()
    await waitFor(() => expect(screen.getByRole('option', { name: 'Low' })).toBeInTheDocument())
    const calls = listAvailableModels.mock.calls.length

    vi.useFakeTimers()
    vi.setSystemTime(Date.now() + 61_000)
    fireEvent.mouseEnter(effort)
    expect(listAvailableModels.mock.calls.length).toBeGreaterThan(calls)
  })
})

describe('probe clock export', () => {
  beforeEach(() => {
    getConfig.mockResolvedValue({ model: 'openrouter::reasoner', reasoning_effort: '' })
    listAvailableModels.mockResolvedValue(byProvider)
    useAgent.setState({ conversationId: null, agents: [], draftScope: null })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('modelListStale is false right after a probe and true after the TTL', async () => {
    render(<ChatScopePickers />)
    await findModelPicker()
    await waitFor(() => expect(modelListStale()).toBe(false))
    vi.useFakeTimers()
    vi.setSystemTime(Date.now() + 61_000)
    expect(modelListStale()).toBe(true)
    vi.useRealTimers()
  })
})
