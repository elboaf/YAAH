// Attached files staged inline (`attachmentText` in components.tsx) are part
// of the outgoing message text the model receives verbatim — but rendering
// that same text raw regurgitates whole files into the chat. The user-message
// renderer collapses attached-file blocks into expandable chips; everything
// else renders byte-for-byte as before, and messages without attachments get
// the plain path (no gratuitous whitespace collapsing of user prose).

import { describe, expect, it, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MessageView } from './components'
import type { ChatMessage } from './store'

afterEach(() => cleanup())

const user = (content: string): ChatMessage => ({ id: 'u1', role: 'user', content })

const inlineBlock = (name: string, body: string) =>
  `\n\n--- attached file: ${name} ---\n\`\`\`\n${body}\n\`\`\``

describe('user message attachment rendering', () => {
  it('collapses an inline attached-file block to a chip — body not shown', () => {
    render(<MessageView msg={user(`look at this${inlineBlock('notes.md', 'secret-line-1\nsecret-line-2')}`)} />)
    expect(screen.getByText(/📎 notes\.md/)).toBeTruthy()
    expect(screen.queryByText('secret-line-1')).toBeNull()
  })

  it('expands on click to show the file body without the fence lines', () => {
    const { container } = render(<MessageView msg={user(`q${inlineBlock('a.txt', 'hello\nworld')}`)} />)
    fireEvent.click(screen.getByText(/📎 a\.txt/))
    expect(container.textContent).toContain('hello\nworld')
    expect(container.textContent).not.toContain('```')
  })

  it('handles two attachments and keeps the surrounding prose', () => {
    const text = `first words${inlineBlock('one.md', 'AAA')}middle${inlineBlock('two.md', 'BBB')}last words`
    const { container } = render(<MessageView msg={user(text)} />)
    expect(screen.getByText(/📎 one\.md/)).toBeTruthy()
    expect(screen.getByText(/📎 two\.md/)).toBeTruthy()
    expect(container.textContent).toContain('first words')
    expect(container.textContent).toContain('middle')
    expect(container.textContent).toContain('last words')
    expect(container.textContent).not.toContain('AAA')
  })

  it('renders messages without attachments as plain text, unchanged', () => {
    const { container } = render(<MessageView msg={user('plain prose\nline two')} />)
    expect(container.querySelector('button')).toBeNull()
    expect(container.textContent).toContain('plain prose\nline two')
  })

  it('does not false-positive on prose that merely mentions the header text', () => {
    // A header with no fenced body following (mid-message, unfenced) is still
    // a real block start — but a message that never contains the pattern at
    // all must stay untouched.
    const { container } = render(<MessageView msg={user('the marker --- attached file: is documented here')} />)
    expect(container.querySelector('button')).toBeNull()
  })
})
