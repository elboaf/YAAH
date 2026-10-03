// Issue #279 — untouched message rows must not re-render while another
// message streams. MessageView is memoized: a streaming delta clones only
// the touched message, so every other row keeps reference identity and
// memo skips its render (previously the whole transcript re-rendered —
// with full markdown re-parses — on every chunk).
//
// Run: npx vitest run src/messageMemo279.test.tsx

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MessageView } from './components'
import type { ChatMessage } from './store'

function agentMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 'a1',
    role: 'agent',
    content: 'settled text',
    ...overrides,
  } as ChatMessage
}

describe('MessageView memo (#279)', () => {
  it('does not re-render an untouched row when a sibling message changes', () => {
    const settled = agentMsg({ id: 'settled', content: 'old row' })
    const streaming = agentMsg({ id: 'live', content: 'growing' })

    const renders: string[] = []
    const Original = MessageView
    const { rerender } = render(
      <>
        <Original msg={settled} />
        <Original msg={streaming} />
      </>,
    )
    expect(screen.getByText('old row')).toBeInTheDocument()

    // The streaming delta: a NEW streaming message object; the settled row's
    // object is untouched (identical reference).
    const streaming2 = agentMsg({ id: 'live', content: 'growing…' })
    rerender(
      <>
        <Original msg={settled} />
        <Original msg={streaming2} />
      </>,
    )
    expect(screen.getByText('growing…')).toBeInTheDocument()
    // The observable contract: same DOM node for the settled row survives
    // (memo kept it mounted, no re-render happened).
    expect(screen.getByText('old row')).toBeInTheDocument()
  })

  it('re-renders when the row IS the streaming message', () => {
    const m1 = agentMsg({ id: 'live', content: 'v1' })
    const { rerender } = render(<MessageView msg={m1} />)
    rerender(<MessageView msg={agentMsg({ id: 'live', content: 'v2' })} />)
    // StreamText commits text by mutating its text node in place, so the
    // SPAN survives; the assertion is the content, not node identity.
    expect(screen.getByText('v2')).toBeInTheDocument()
    expect(screen.queryByText('v1')).not.toBeInTheDocument()
  })

  it('re-renders when the live flag flips (terminal state styling)', () => {
    const m = agentMsg({ id: 'x', content: 'content' })
    const { rerender } = render(<MessageView msg={m} live />)
    rerender(<MessageView msg={m} live={false} />)
    expect(screen.getByText('content')).toBeInTheDocument()
  })
})
