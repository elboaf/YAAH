// #207 Feature B — "Show <say> emissions in chat": the captured briefing
// renders as a visibly distinct spoken line on messages that also carry
// chat text, only when the toggle is on. The say-only fallback (empty chat
// content) is NOT governed by this toggle — the briefing IS the body there
// (src/sayOnlyEmission.test.tsx) and must render either way, unduplicated.
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { MessageView } from './components'
import { useAgent, type ChatMessage } from './store'

const base = { id: 'm1', role: 'assistant' as const }

afterEach(() => {
  cleanup()
  useAgent.setState({ sayInChat: false })
})

describe('show <say> emissions in chat (#207)', () => {
  it('hides the briefing by default (default: unchecked)', () => {
    const msg: ChatMessage = {
      ...base,
      content: 'Real chat text.',
      say: 'Briefing line.',
    }
    render(<MessageView msg={msg} />)
    expect(screen.getByText('Real chat text.')).toBeInTheDocument()
    expect(screen.queryByText('Briefing line.')).not.toBeInTheDocument()
  })

  it('shows the briefing as a distinct spoken line when enabled', () => {
    useAgent.setState({ sayInChat: true })
    const msg: ChatMessage = {
      ...base,
      content: 'Real chat text.',
      say: 'Briefing line.',
    }
    const { container } = render(<MessageView msg={msg} />)
    expect(screen.getByText('Briefing line.')).toBeInTheDocument()
    // Visibly distinct: muted/italic, not conflated with chat prose.
    expect(container.querySelector('em.say-line')).not.toBeNull()
  })

  it('never duplicates the say-only fallback body when enabled', () => {
    useAgent.setState({ sayInChat: true })
    const msg: ChatMessage = {
      ...base,
      content: '',
      say: 'Checked the existing course folders, now building the Ghidra track.',
      toolCalls: [
        { id: 't1', name: 'bash', args: { command: 'ls' }, startedAt: 1, result: { exit_code: 0 } },
      ],
    }
    render(<MessageView msg={msg} />)
    const shown = screen.getAllByText(/Checked the existing course folders/)
    expect(shown).toHaveLength(1)
    expect(document.querySelector('em.say-line')).toBeNull()
  })
})
