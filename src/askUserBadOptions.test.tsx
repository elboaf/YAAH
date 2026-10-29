// #564: a malformed ask_user `options` payload (model-owned) used to reach
// the card's `.map(...)` and unmount the whole React tree — the uniform
// grey window. These tests pin (a) the normalizer's coercion table,
// (b) normalization at the store ingestion point (tape path), and
// (c) the root ErrorBoundary that turns any residual render throw into a
// captured error with a working Reload button instead of a dead webview.

import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { tapeQuestionAction } from './store'
import { normalizeAskOptions } from './askOptions'
import { ErrorBoundary } from './ErrorBoundary'

describe('normalizeAskOptions', () => {
  it('passes a well-formed options array through', () => {
    const good = [{ label: 'Yes' }, { label: 'No', description: 'skip' }]
    expect(normalizeAskOptions(good)).toEqual(good)
  })

  it('coerces every non-array shape to []', () => {
    expect(normalizeAskOptions(undefined)).toEqual([])
    expect(normalizeAskOptions(null)).toEqual([])
    expect(normalizeAskOptions('Pepperoni')).toEqual([])
    expect(normalizeAskOptions({ label: 'x' })).toEqual([])
  })

  it('drops array entries without a string label instead of throwing', () => {
    expect(normalizeAskOptions([{ nope: 1 }, null, 'x', { label: 'Keep' }])).toEqual([
      { label: 'Keep' },
    ])
  })

  it('keeps only string descriptions', () => {
    expect(normalizeAskOptions([{ label: 'A', description: 5 }])).toEqual([{ label: 'A' }])
  })
})

describe('tapeQuestionAction (#564 ingestion)', () => {
  it('normalizes a string options payload to [] instead of passing it through', () => {
    const act = tapeQuestionAction(
      {
        type: 'tool_start',
        name: 'ask_user',
        call_id: 'q1',
        args: { question: 'Pizza?', options: 'Pepperoni' },
      },
      '42',
    )
    expect(act).toEqual({
      op: 'set',
      callId: 'q1',
      question: 'Pizza?',
      options: [],
      convKey: '42',
    })
  })

  it('drops malformed entries from an array payload', () => {
    const act = tapeQuestionAction(
      {
        type: 'tool_start',
        name: 'ask_user',
        call_id: 'q2',
        args: { question: 'Q', options: [{ label: 'Ok' }, 'garbage'] },
      },
      '42',
    )
    expect(act && act.op === 'set' ? act.options : null).toEqual([{ label: 'Ok' }])
  })
})

describe('ErrorBoundary (#564 last line of defense)', () => {
  function Bomb() {
    throw new Error('render boom')
  }

  it('renders the fallback with the error text and a Reload button instead of unmounting', () => {
    // React logs the caught error; silence the expected console noise.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    )
    expect(screen.getByText(/hit an error it could not render/i)).toBeTruthy()
    expect(screen.getByText(/render boom/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /reload/i })).toBeTruthy()
    spy.mockRestore()
  })

  it('the Reload button reloads the window', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const reload = vi.fn()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, reload },
    })
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    )
    fireEvent.click(screen.getByRole('button', { name: /reload/i }))
    expect(reload).toHaveBeenCalledTimes(1)
    spy.mockRestore()
  })
})
