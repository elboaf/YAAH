// Chip render test (#142): a user message carrying structured attachments
// renders one filename+size chip per attachment and no inline content blob.
// Run: npx vitest run src/attachmentChipRender.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MessageView } from './components'
import type { ChatMessage } from './store'

const msg: ChatMessage = {
  id: 'm1',
  role: 'user',
  content: 'please review',
  attachments: [
    { name: 'notes.md', size: 8, content: '# hello\n' },
    { name: 'big.log', size: 204_800, path: '.yaah-attachments/big.log' },
  ],
}

describe('attachment chips in the user bubble (#142)', () => {
  it('renders a filename+size chip per attachment', () => {
    render(<MessageView msg={msg} />)
    expect(screen.getByText('notes.md')).toBeTruthy()
    expect(screen.getByText('big.log')).toBeTruthy()
    expect(screen.getByText('· 205 KB')).toBeTruthy()
  })

  it('never renders the attachment content as text', () => {
    const { container } = render(<MessageView msg={msg} />)
    expect(container.textContent).not.toContain('# hello')
  })

  it('keeps the user’s own words visible', () => {
    render(<MessageView msg={msg} />)
    expect(screen.getByText('please review')).toBeTruthy()
  })
})
