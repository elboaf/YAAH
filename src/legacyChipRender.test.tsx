// Legacy chip display test (#144): old conversations' raw legacy-concatenated
// messages render as chips at display time — no DB rewrite, nothing else
// changes. Lookalike text and ambiguous blobs render raw exactly as today.
// Run: npx vitest run src/legacyChipRender.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MessageView } from './components'
import type { ChatMessage } from './store'
import { attachmentText } from './attachments'
import { FIXTURE } from './attachmentFixture'

const mk = (content: string): ChatMessage => ({
  id: 'm1',
  role: 'user',
  content,
})

const legacyInline = mk('please review' + attachmentText(FIXTURE[0][0]))
const legacyStaged = mk('check this' + attachmentText(FIXTURE[1][0]))

describe('legacy messages render as chips (#144)', () => {
  it('renders inline legacy blobs as chips, content hidden from the text', () => {
    const { container } = render(<MessageView msg={legacyInline} />)
    expect(screen.getByText('notes.md')).toBeTruthy()
    expect(screen.getByText('please review')).toBeTruthy()
    expect(container.textContent).not.toContain('# hello')
    expect(container.textContent).not.toContain('--- attached file:')
  })

  it('renders staged pointer messages as chips (lazy fetch on expand)', () => {
    const { container } = render(<MessageView msg={legacyStaged} />)
    expect(screen.getByText('big.log')).toBeTruthy()
    expect(screen.getByText('check this')).toBeTruthy()
    expect(container.textContent).not.toContain('--- attached file:')
    expect(container.textContent).not.toContain('Saved to')
  })

  it('expands a legacy inline chip like a structured one (#143 behavior)', () => {
    render(<MessageView msg={legacyInline} />)
    fireEvent.click(screen.getByText('notes.md'))
    expect(screen.getByText('# hello')).toBeTruthy()
  })

  it('renders hand-typed lookalikes untouched', () => {
    const lookalike = mk(
      'I typed this myself\n\nlook --- attached file: fake.txt ---\n```\nhi\n```',
    )
    const { container } = render(<MessageView msg={lookalike} />)
    expect(screen.queryByText('fake.txt')).toBeNull()
    expect(container.textContent).toContain('--- attached file: fake.txt ---')
  })

  it('renders ambiguous blobs raw, silently', () => {
    const ambiguous = mk(
      'x\n\n--- attached file: tricky.txt ---\n```\ncode:\n```\nmore\n```',
    )
    const { container } = render(<MessageView msg={ambiguous} />)
    expect(screen.queryByText('tricky.txt')).toBeNull()
    expect(container.textContent).toContain('--- attached file:')
  })

  it('prefers structured attachments when the row already has them', () => {
    const both: ChatMessage = {
      ...legacyInline,
      attachments: [{ name: 'structured.txt', size: 3, content: 'abc' }],
    }
    const { container } = render(<MessageView msg={both} />)
    expect(screen.getByText('structured.txt')).toBeTruthy()
    expect(screen.queryByText('notes.md')).toBeNull()
    // Raw content stays visible: the legacy text is part of msg.content and
    // structured rows replay it verbatim — we must not strip anything.
    expect(container.textContent).toContain('notes.md')
  })
})
