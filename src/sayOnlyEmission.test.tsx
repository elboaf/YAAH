// Regression test: a say-only emission (all text inside the <say> briefing,
// no chat text before the tag) persisted as an empty assistant row — a blank
// line above the tool calls in every affected transcript. The briefing is
// readable prose: the message body must fall back to it when chat content is
// empty. Chosen via ask_user (fallback = briefing-as-chat).

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MessageView } from './components'
import type { ChatMessage } from './store'

const base = { id: 'm1', role: 'assistant' as const }

describe('say-only emission fallback', () => {
  it('renders the briefing as chat when content is empty but say exists', () => {
    const msg: ChatMessage = {
      ...base,
      content: '',
      say: 'Checked the existing course folders, now building the Ghidra track.',
      toolCalls: [
        { id: 't1', name: 'bash', args: { command: 'ls' }, startedAt: 1, result: { exit_code: 0 } },
      ],
    }
    render(<MessageView msg={msg} />)
    expect(
      screen.getByText(/Checked the existing course folders/),
    ).toBeInTheDocument()
  })

  it('keeps normal chat content authoritative over the briefing', () => {
    const msg: ChatMessage = {
      ...base,
      content: 'Real chat text.',
      say: 'Briefing line.',
    }
    render(<MessageView msg={msg} />)
    expect(screen.getByText('Real chat text.')).toBeInTheDocument()
    expect(screen.queryByText('Briefing line.')).not.toBeInTheDocument()
  })

  it('still renders the blank-cursor pulse for a truly empty live message', () => {
    const msg: ChatMessage = { ...base, content: '' }
    const { container } = render(<MessageView msg={msg} live />)
    expect(container.querySelector('.run-pulse')).not.toBeNull()
  })
})
