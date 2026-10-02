// Agent-prompt chip tests (#198): a scheduled agent fire persists its
// effective prompt as a tagged user row; the transcript renders it as a
// collapsed chip (not the full text), click expands the verbatim per-run
// prompt inline, click again collapses. Hand-typed user messages (no tag)
// render as normal bubbles.
// Run: npx vitest run src/agentPromptChip.test.tsx
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MessageView } from './components'

afterEach(() => {
  cleanup()
})

describe('agent prompt chip (#198)', () => {
  const run1 = {
    id: 'db1',
    role: 'user' as const,
    content: 'summarize commits\n\n# Standing instructions\n\n- keep it under 10 lines',
    meta: { agent_prompt: true },
  }
  const run2 = {
    id: 'db2',
    role: 'user' as const,
    content: 'triage the issue inbox\n\n# Standing instructions\n\n- ignore draft PRs',
    meta: { agent_prompt: true },
  }

  it('renders collapsed by default — full prompt text is not shown', () => {
    const { container } = render(<MessageView msg={run1} />)
    // The chip is there…
    expect(screen.getByText(/Agent prompt/)).toBeTruthy()
    // …but the verbatim prompt body is not rendered.
    expect(container.textContent).not.toContain('Standing instructions')
    expect(container.querySelector('[data-agent-prompt-expand]')).toBeNull()
  })

  it('click expands the exact per-run prompt; click again collapses', () => {
    const { container } = render(<MessageView msg={run1} />)
    fireEvent.click(screen.getByText(/Agent prompt/))
    const block = container.querySelector('[data-agent-prompt-expand]') as HTMLElement
    expect(block).toBeTruthy()
    expect(block.textContent).toBe(run1.content)
    fireEvent.click(screen.getByText(/Agent prompt/))
    expect(container.querySelector('[data-agent-prompt-expand]')).toBeNull()
  })

  it('two runs keep their own per-run copies after an agent edit', () => {
    const { container } = render(
      <div>
        <MessageView msg={run1} />
        <MessageView msg={run2} />
      </div>,
    )
    const chips = screen.getAllByText(/Agent prompt/)
    expect(chips.length).toBe(2)
    fireEvent.click(chips[0])
    let blocks = container.querySelectorAll('[data-agent-prompt-expand]')
    expect(blocks.length).toBe(1)
    expect(blocks[0].textContent).toBe(run1.content)
    fireEvent.click(chips[1])
    blocks = container.querySelectorAll('[data-agent-prompt-expand]')
    expect(blocks.length).toBe(2)
    expect(blocks[1].textContent).toBe(run2.content)
    expect(blocks[1].textContent).not.toBe(blocks[0].textContent)
  })

  it('hand-typed user messages render as a normal bubble, no chip', () => {
    const typed = { id: 'db3', role: 'user' as const, content: 'hello there' }
    const { container } = render(<MessageView msg={typed} />)
    expect(screen.queryByText(/Agent prompt/)).toBeNull()
    expect(container.querySelector('[data-agent-prompt-expand]')).toBeNull()
    expect(container.textContent).toContain('hello there')
  })
})
