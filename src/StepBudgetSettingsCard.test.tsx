// #838 — Global agent step budget field. A pure draft: renders a single
// "Max steps" input, edits through onChange, and ships the draft through
// the Settings modal's Save payload (max_steps). '' = untouched (shipped
// default applies); 0 = unlimited.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { StepBudgetSettingsCard } from './components'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('global step budget field (#838)', () => {
  it('renders a Max steps input bound to the draft', () => {
    render(<StepBudgetSettingsCard draft={200} onChange={vi.fn()} />)
    expect(screen.getByLabelText('Global max steps')).toHaveValue(200)
  })

  it('edits through onChange, not a callback prop', () => {
    const onChange = vi.fn()
    render(<StepBudgetSettingsCard draft={200} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText('Global max steps'), {
      target: { value: '500' },
    })
    expect(onChange).toHaveBeenCalledWith(500)
  })

  it('accepts a blank field (default applies)', () => {
    const onChange = vi.fn()
    render(<StepBudgetSettingsCard draft={200} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText('Global max steps'), {
      target: { value: '' },
    })
    expect(onChange).toHaveBeenCalledWith('')
  })

  it('accepts 0 (unlimited)', () => {
    const onChange = vi.fn()
    render(<StepBudgetSettingsCard draft={200} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText('Global max steps'), {
      target: { value: '0' },
    })
    expect(onChange).toHaveBeenCalledWith(0)
  })
})
