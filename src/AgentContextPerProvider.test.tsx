import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { AgentContextPerProvider, providerEntryModels, type ModelEntry } from './AgentContextPerProvider'

const base = {
  name: 'acme',
  models: ['m-alpha', 'm-beta', 'm-gamma'],
  maxStepsDefault: 200,
  inputCls: '',
}

function entry(over: Partial<ModelEntry> = {}): ModelEntry {
  return {
    model: 'm-alpha',
    steps: 200,
    ctxAuto: 128000,
    onSteps: vi.fn(),
    ...over,
  }
}

describe('per-model Agent & context editor (#111)', () => {
  it('renders one independent entry per configured model', () => {
    render(
      <AgentContextPerProvider
        {...base}
        entries={[
          entry(),
          entry({ model: 'm-beta', steps: 42 }),
        ]}
        onAddModel={vi.fn()}
      />,
    )
    // Each entry shows its own steps, labeled by model.
    expect(screen.getByLabelText('max steps for m-alpha')).toHaveValue(200)
    expect(screen.getByLabelText('max steps for m-beta')).toHaveValue(42)
  })

  it('edits go to the entry they belong to', () => {
    const onStepsA = vi.fn()
    const onStepsB = vi.fn()
    render(
      <AgentContextPerProvider
        {...base}
        entries={[entry({ onSteps: onStepsA }), entry({ model: 'm-beta', onSteps: onStepsB })]}
        onAddModel={vi.fn()}
      />,
    )
    fireEvent.change(screen.getByLabelText('max steps for m-beta'), { target: { value: '7' } })
    expect(onStepsB).toHaveBeenCalledWith(7)
    expect(onStepsA).not.toHaveBeenCalled()
  })

  it('adds an entry via the catalog dropdown', () => {
    const onAddModel = vi.fn()
    render(
      <AgentContextPerProvider
        {...base}
        entries={[entry()]}
        onAddModel={onAddModel}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: /add a model to configure/ }))
    // The dropdown lists catalog models (not already configured entries).
    fireEvent.change(screen.getByLabelText('model to add for acme'), {
      target: { value: 'm-gamma' },
    })
    expect(onAddModel).toHaveBeenCalledWith('m-gamma')
  })

  it('keeps a free-type fallback for ids outside the catalog', () => {
    const onAddModel = vi.fn()
    render(
      <AgentContextPerProvider
        {...base}
        entries={[]}
        onAddModel={onAddModel}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: /add a model to configure/ }))
    fireEvent.click(screen.getByRole('button', { name: 'type a model id instead' }))
    const input = screen.getByLabelText('model id to add for acme')
    fireEvent.change(input, { target: { value: ' openai/gpt-5.2 ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onAddModel).toHaveBeenCalledWith('openai/gpt-5.2')
  })

  it('has no provider-level max steps field', () => {
    render(
      <AgentContextPerProvider
        {...base}
        entries={[entry()]}
        onAddModel={vi.fn()}
      />,
    )
    // The only "Max steps" inputs are the per-entry ones.
    const labels = screen.getAllByText('max steps')
    expect(labels.length).toBe(1)
    expect(labels[0].closest('[data-model-entry]')).toBeTruthy()
  })
})

describe('provider entry grouping', () => {
  const TIEL = 'Tiel-Coder-35B-A3B-MTP-UD-Q4_K_S'

  it('claims saved entries under sibling slot prefixes of the default model', () => {
    const ids = providerEntryModels(
      [`1.${TIEL}`, 'x2.qwen36-reap50-Q4_K_M-imat', 'gpt-4o'],
      `x2.${TIEL}`,
      [],
    )
    // The 1.Tiel entry covers this provider (sibling prefix of the x2.Tiel
    // default); the qwen36 and gpt-4o entries are other models'.
    expect(ids).toEqual([`1.${TIEL}`, `x2.${TIEL}`])
  })

  it('claims saved entries matching any catalog model family', () => {
    const ids = providerEntryModels(
      [`1.${TIEL}`, 'gpt-4o'],
      'some-other-local-model',
      [`x2.${TIEL}`, 'glm-5.3-flash'],
    )
    // The 1.Tiel entry is claimed via the catalog family; the default
    // model itself is still listed.
    expect(ids).toEqual([`1.${TIEL}`, 'some-other-local-model'])
    expect(ids).not.toContain('gpt-4o')
  })

  it('always lists the default model, even with no entry', () => {
    const ids = providerEntryModels([], 'mystery-model', [])
    expect(ids).toEqual(['mystery-model'])
  })
})
