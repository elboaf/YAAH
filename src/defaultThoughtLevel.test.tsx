import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { getConfig, updateConfig, listAvailableModels } = vi.hoisted(() => ({
  getConfig: vi.fn(),
  updateConfig: vi.fn(),
  listAvailableModels: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, getConfig, updateConfig, listAvailableModels }
})

import { DefaultThoughtLevelPicker } from './components'
import { useAgent } from './store'

describe('default thought level picker (#98)', () => {
  beforeEach(() => {
    getConfig.mockResolvedValue({ model: 'openrouter::reasoner', reasoning_effort: 'high' })
    listAvailableModels.mockResolvedValue({
      providers: {
        openrouter: {
          models: ['reasoner'],
          model_info: [{ id: 'reasoner', reasoning_efforts: ['low', 'high', 'max'], supports_reasoning: true }],
        },
      },
    })
    updateConfig.mockImplementation(async (patch: { reasoning_effort: string }) => {
      getConfig.mockResolvedValue({ model: 'openrouter::reasoner', reasoning_effort: patch.reasoning_effort })
      return { ok: true }
    })
    useAgent.setState({ globalModel: 'openrouter::reasoner', globalEffort: 'high' })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('saves the selected thought level as the default for new chats', async () => {
    render(<DefaultThoughtLevelPicker />)
    const picker = screen.getByRole('combobox', { name: 'Thought level' })
    await waitFor(() => expect(screen.getByRole('option', { name: 'Max' })).toBeInTheDocument())
    expect(picker).toHaveValue('high')

    fireEvent.change(picker, { target: { value: 'low' } })

    await waitFor(() => expect(updateConfig).toHaveBeenCalledWith({ reasoning_effort: 'low' }))
    await waitFor(() => expect(useAgent.getState().globalEffort).toBe('low'))
    await waitFor(() => expect(picker).toHaveValue('low'))
  })

  it('offers the provider default plus exactly the selected model advertised levels', async () => {
    render(<DefaultThoughtLevelPicker />)
    const picker = screen.getByRole('combobox', { name: 'Thought level' })
    await waitFor(() => expect(screen.getByRole('option', { name: 'Max' })).toBeInTheDocument())
    expect(picker).toHaveValue('high')
    expect(screen.getByRole('option', { name: 'Default' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Low' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'High' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Medium' })).not.toBeInTheDocument()
  })
})
