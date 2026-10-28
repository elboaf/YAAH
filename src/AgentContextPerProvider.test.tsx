import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { AgentContextPerProvider, providerEntryModels, type ModelEntry } from './AgentContextPerProvider'

const base = {
  name: 'acme',
  models: ['m-alpha', 'm-beta', 'm-gamma'],
  compactionDefaultK: 300,
  maxStepsDefault: 200,
  inputCls: '',
}

function entry(over: Partial<ModelEntry> = {}): ModelEntry {
  return {
    model: 'm-alpha',
    steps: 200,
    compEnabled: true,
    compK: 300,
    ctxAuto: 128000,
    effective: null,
    entryKey: null,
    siblings: [],
    onSteps: vi.fn(),
    onCompEnabled: vi.fn(),
    onCompK: vi.fn(),
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
          entry({ model: 'm-beta', steps: 42, compEnabled: false, compK: 100 }),
        ]}
        onAddModel={vi.fn()}
      />,
    )
    // Each entry shows its own steps + compaction controls, labeled by model.
    expect(screen.getByLabelText('max steps for m-alpha')).toHaveValue(200)
    expect(screen.getByLabelText('max steps for m-beta')).toHaveValue(42)
    expect(
      screen.getByRole('switch', { name: 'Enable history compaction for m-alpha' }),
    ).toHaveAttribute('aria-checked', 'true')
    expect(
      screen.getByRole('switch', { name: 'Enable history compaction for m-beta' }),
    ).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByLabelText('Compaction trigger for m-beta')).toHaveValue(100)
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

describe('effective-behavior readout (#588 fault B)', () => {
  it('says "fires at Xk (via KEY)" when an entry covers the model', () => {
    render(
      <AgentContextPerProvider
        {...base}
        entries={[
          entry({
            model: 'x2.Tiel-Coder-35B-A3B-MTP-UD-Q4_K_S',
            effective: { enabled: true, triggerK: 24 },
            entryKey: 'x2.qwen36-reap50-Q4_K_M-imat',
            siblings: ['x2.qwen36-reap50-Q4_K_M-imat'],
          }),
        ]}
        onAddModel={vi.fn()}
      />,
    )
    const line = screen.getByText(/fires at 24k/)
    expect(line.textContent).toContain('via x2.qwen36-reap50-Q4_K_M-imat')
    expect(line.textContent).toContain('shared by 1 alias')
  })

  it('says "no entry — auto fallback" when the model has no entry', () => {
    render(
      <AgentContextPerProvider
        {...base}
        entries={[entry({ effective: { enabled: true, triggerK: 105 }, entryKey: null })]}
        onAddModel={vi.fn()}
      />,
    )
    expect(screen.getByText('no entry — auto fallback (~105k)')).toBeTruthy()
  })

  it('says the model keeps its full history when its entry is disabled', () => {
    render(
      <AgentContextPerProvider
        {...base}
        entries={[entry({ effective: { enabled: false, triggerK: 0 }, entryKey: 'local-model' })]}
        onAddModel={vi.fn()}
      />,
    )
    expect(screen.getByText('compaction off — this model keeps its full history')).toBeTruthy()
  })

  it('renders nothing about behavior while the probe has no answer', () => {
    render(
      <AgentContextPerProvider
        {...base}
        entries={[entry({ effective: null, entryKey: null })]}
        onAddModel={vi.fn()}
      />,
    )
    expect(screen.queryByText(/fires at/)).toBeNull()
    expect(screen.queryByText(/auto fallback/)).toBeNull()
  })
})

describe('provider entry grouping (#588 fault A)', () => {
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
    // model itself is still listed (no entry -> auto-fallback readout).
    expect(ids).toEqual([`1.${TIEL}`, 'some-other-local-model'])
    expect(ids).not.toContain('gpt-4o')
  })

  it('always lists the default model, even with no entry', () => {
    const ids = providerEntryModels([], 'mystery-model', [])
    expect(ids).toEqual(['mystery-model'])
  })
})
