import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MessageView } from './components'
import type { ChatMessage } from './store'

afterEach(() => cleanup())

describe('system note row (#428)', () => {
  it('renders a note system row as plain text, not raw JSON', () => {
    const msg: ChatMessage = {
      id: 'note-1',
      role: 'system',
      content: JSON.stringify({
        note: 'Provider rejected image input - 1 image(s) stripped from context, retrying text-only with the file paths intact.',
      }),
    }
    render(<MessageView msg={msg} />)
    expect(screen.getByText(/Provider rejected image input/)).toBeTruthy()
    expect(screen.queryByText(/attached_image|stored rel|JSON/)).toBeNull()
  })

  it('still renders skill_not_found rows (no note-key collision)', () => {
    const msg: ChatMessage = {
      id: 'snf-1',
      role: 'system',
      content: JSON.stringify({ skill_not_found: { skills: ['nope'] } }),
    }
    render(<MessageView msg={msg} />)
    expect(screen.getByText(/Skill not found/)).toBeTruthy()
  })
})
