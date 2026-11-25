import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ObjectiveCard } from './components'

afterEach(() => cleanup())

describe('objective card (#844 follow-up)', () => {
  it('shows the durable objective', () => {
    render(<ObjectiveCard objective="add tests for the auth module" iteration={1} />)
    expect(screen.getByText('add tests for the auth module')).toBeTruthy()
  })

  it('shows no iteration badge on the first attempt', () => {
    render(<ObjectiveCard objective="add tests for the auth module" iteration={1} />)
    expect(screen.queryByText(/iteration 1/)).toBeNull()
  })

  it('surfaces the verifier-loop iteration once it has run past the first', () => {
    render(<ObjectiveCard objective="add tests for the auth module" iteration={3} />)
    expect(screen.getByText(/iteration 3/)).toBeTruthy()
  })
})
