import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MessageView } from './components'
import type { ChatMessage } from './store'

afterEach(() => cleanup())

describe('goal row (#844)', () => {
  it('renders a verification-failed goal row with reason and steer', () => {
    const msg: ChatMessage = {
      id: 'goal-1',
      role: 'system',
      content: JSON.stringify({
        goal: {
          phase: 'verification_failed',
          passed: false,
          reason: 'no artifact produced',
          nextAction: 'write src/thing.py',
        },
      }),
    }
    render(<MessageView msg={msg} />)
    expect(screen.getByText(/Goal not verified/)).toBeTruthy()
    expect(screen.getByText(/no artifact produced/)).toBeTruthy()
    expect(screen.getByText(/write src\/thing\.py/)).toBeTruthy()
    expect(screen.queryByText(/JSON/)).toBeNull()
  })

  it('renders a passing-iteration goal row without a reason', () => {
    const msg: ChatMessage = {
      id: 'goal-2',
      role: 'system',
      content: JSON.stringify({
        goal: { phase: 'iteration', passed: true, reason: '', nextAction: 'iterate on styling' },
      }),
    }
    render(<MessageView msg={msg} />)
    expect(screen.getByText(/Iteration/)).toBeTruthy()
    expect(screen.getByText(/iterate on styling/)).toBeTruthy()
  })
})
