import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { ContextChip } from './components'

function info(tokens: number) {
  return { tokens, window: 200_000, model: 'test-model' }
}

const TOOLTIP_LINES = [
  'This is the recommended max context tracker.',
  'This is not a rule, but starting a new chat is recommended before the dial reaches 100%',
  'Over 120k context causes inefficient, inconsistent, and generally poor model behavior, not to mention higher token usage.',
]

function svgOf(container: HTMLElement) {
  const svg = container.querySelector('svg')
  if (!svg) throw new Error('dial svg not rendered')
  return svg
}

describe('ContextChip context dial (#135)', () => {
  it('shows the unclamped hub percentage past 100% and wraps the arc (250k -> 125%, 25% into second lap)', () => {
    const { container } = render(<ContextChip info={info(250_000)} />)
    const svg = svgOf(container)
    expect(svg.querySelector('text')!.textContent).toBe('125%')
    const path = svg.querySelector('path')!.getAttribute('d')!
    // Wrapped: arc ends at 25% of the circle from 12 o'clock, largeArc = 0.
    const r = 8.5
    const endAngle = 0.25 * 2 * Math.PI - Math.PI / 2
    const endX = (12 + r * Math.cos(endAngle)).toFixed(3)
    const endY = (12 + r * Math.sin(endAngle)).toFixed(3)
    expect(path).toBe(`M 12 ${12 - r} A ${r} ${r} 0 0 1 ${endX} ${endY}`)
    // Wrapped: largeArc = 0 (an unwrapped 125% would be a >half-circle, largeArc = 1).
    expect(path).toContain(' 0 0 1 ')
  })

  it('never clamps the hub readout (500k -> 250%)', () => {
    const { container } = render(<ContextChip info={info(500_000)} />)
    expect(svgOf(container).querySelector('text')!.textContent).toBe('250%')
  })

  it('renders identically below 100% (100k -> 100%... uses wrapped frac = raw frac)', () => {
    const { container } = render(<ContextChip info={info(100_000)} />)
    const svg = svgOf(container)
    expect(svg.querySelector('text')!.textContent).toBe('50%')
    // Below 100%: largeArc = 0, same geometry as before the change.
    const r = 8.5
    const endAngle = 0.5 * 2 * Math.PI - Math.PI / 2
    const endX = (12 + r * Math.cos(endAngle)).toFixed(3)
    const endY = (12 + r * Math.sin(endAngle)).toFixed(3)
    expect(svg.querySelector('path')!.getAttribute('d')).toBe(
      `M 12 ${12 - r} A ${r} ${r} 0 0 1 ${endX} ${endY}`,
    )
    // Threshold ticks unchanged: compaction off by default -> only the 120k dumb-zone tick.
    const lines = svg.querySelectorAll('line')
    expect(lines).toHaveLength(1)
    expect(lines[0].getAttribute('stroke')).toBe('#f87171')
  })

  it('tooltip is exactly the three copy lines, old numeric lines gone', () => {
    const { container } = render(<ContextChip info={info(250_000)} />)
    const chip = container.querySelector('span[title]')!
    expect(chip.getAttribute('title')).toBe(TOOLTIP_LINES.join('\n'))
    expect(chip.getAttribute('title')).not.toContain('dial scaled to')
    expect(chip.getAttribute('title')).not.toContain('dumb zone from')
  })
})
