import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { ContextChip } from './components'

function info(tokens: number, cost: number | null = null) {
  return { tokens, window: 200_000, model: 'test-model', cost }
}

// The circular context dial was retired in the composer-condensation round:
// ContextChip is now the bare token count (no units), color-stepped by
// threshold bands, with the dumb-zone guidance and billed cost in the
// tooltip. These tests pin that contract.

describe('ContextChip condensed counter (composer condensation round)', () => {
  it('renders the bare token count with no units and no dial svg', () => {
    const { container } = render(<ContextChip info={info(25_900)} />)
    expect(container.textContent).toBe('25.9k')
    expect(container.querySelector('svg')).toBeNull()
    expect(container.textContent).not.toContain('tok')
    expect(container.textContent).not.toContain('%')
  })

  it('renders nothing before the first turn completes (no usage yet)', () => {
    const { container } = render(<ContextChip info={undefined} />)
    expect(container.textContent).toBe('')
  })

  it('steps the count color by threshold band: blue -> yellow -> orange -> red', () => {
    const chipClass = (tokens: number) => {
      const { container } = render(<ContextChip info={info(tokens)} />)
      const chip = container.querySelector('span')
      return chip?.className ?? ''
    }
    expect(chipClass(25_900)).toContain('text-blue-600')
    expect(chipClass(100_000)).toContain('text-yellow-500')
    expect(chipClass(120_000)).toContain('text-orange-500')
    expect(chipClass(200_000)).toContain('text-red-500')
  })

  it('tooltip keeps the dumb-zone guidance with the 120k figure', () => {
    const { container } = render(<ContextChip info={info(125_000)} />)
    const chip = container.querySelector('span[title]')!
    expect(chip.getAttribute('title')).toContain('~120k')
    expect(chip.getAttribute('title')).toContain('Starting a new chat is recommended')
  })

  it('shows the billed cost in the tooltip and inline only when the provider reports one', () => {
    const { container } = render(<ContextChip info={info(25_900, 0.0123)} />)
    expect(container.textContent).toContain('$0.0123')
    expect(container.querySelector('span[title]')!.getAttribute('title')).toContain('Session cost: $0.0123')

    const { container: noCost } = render(<ContextChip info={info(25_900, null)} />)
    expect(noCost.textContent).toBe('25.9k')
    expect(noCost.textContent).not.toContain('$')
  })
})
