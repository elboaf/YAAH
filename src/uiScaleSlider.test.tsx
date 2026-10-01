// #171 — Interface scale: the four preset buttons become a live 100–200%
// slider. The card is exported for isolation (SaySettingsCard precedent);
// the Settings modal embeds it and applies each change immediately as a
// ui-scale-changed event (preview-while-sliding), persisting on Save.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { InterfaceScaleCard } from './components'

afterEach(() => cleanup())

describe('interface-scale slider (#171)', () => {
  it('renders a range input bounded to 100–200% with the current % labeled', () => {
    render(<InterfaceScaleCard scale={1.25} onChange={() => {}} />)
    const slider = screen.getByRole('slider', { name: /interface scale/i })
    expect(slider.getAttribute('min')).toBe('1')
    expect(slider.getAttribute('max')).toBe('2')
    expect((slider as HTMLInputElement).value).toBe('1.25')
    expect(screen.getByText('125%')).toBeTruthy()
  })

  it('reports quantized values from dragging and arrow keys', () => {
    const seen: number[] = []
    render(<InterfaceScaleCard scale={1} onChange={(s) => seen.push(s)} />)
    const slider = screen.getByRole('slider', { name: /interface scale/i })
    fireEvent.change(slider, { target: { value: '1.7319' } })
    expect(seen).toEqual([1.73]) // quantizeUiScale rounds to 2 decimals (#133)
    fireEvent.change(slider, { target: { value: '2' } })
    expect(seen[1]).toBe(2)
  })

  it('clamps the displayed % label when a parent value arrives out of range', () => {
    render(<InterfaceScaleCard scale={9.9} onChange={() => {}} />)
    expect(screen.getByText('200%')).toBeTruthy()
  })
})
