import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { getConfig, updateConfig } = vi.hoisted(() => ({
  getConfig: vi.fn(),
  updateConfig: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, getConfig, updateConfig }
})

import { DefaultThoughtLevelPicker } from './components'
import { useAgent } from './store'

describe('default thought level picker (#98)', () => {
  beforeEach(() => {
    getConfig.mockResolvedValue({ reasoning_effort: 'high' })
    updateConfig.mockResolvedValue({ ok: true })
    useAgent.setState({ globalEffort: 'medium' })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('saves the selected thought level as the default for new chats', async () => {
    render(<DefaultThoughtLevelPicker />)
    const picker = screen.getByRole('combobox', { name: 'Thought level' })
    expect(picker).toHaveValue('medium')

    fireEvent.change(picker, { target: { value: 'high' } })

    await waitFor(() => expect(updateConfig).toHaveBeenCalledWith({ reasoning_effort: 'high' }))
    await waitFor(() => expect(useAgent.getState().globalEffort).toBe('high'))
    expect(picker).toHaveValue('high')
  })

  it('offers the provider default and all supported thought levels without extra helper copy', () => {
    render(<DefaultThoughtLevelPicker />)
    const picker = screen.getByRole('combobox', { name: 'Thought level' })
    expect(picker).toHaveDisplayValue('Medium')
    expect(screen.getByRole('option', { name: 'Default' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Low' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Medium' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'High' })).toBeInTheDocument()
    expect(screen.queryByText('Only affects reasoning-capable models.')).not.toBeInTheDocument()
  })
})
