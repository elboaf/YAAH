// Issue #255: a conversation whose title still carries provider-injected
// `<system_*>` control text (the provider's low-context warning captured by
// the title slice before the sanitize fix) renders a dedicated amber
// warning triangle in the row's #25 status slot, left-justified like the
// run-dots/run-bar family and color-matched to the working dots
// (text-amber-400). The provider text itself never occupies the sidebar:
// the tooltip carries the full warning plus the actionable suggestion to
// run /handoff and start a new chat.

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import { ConversationRow } from './components'

const renderConv = (title: string) =>
  render(
    <ConversationRow
      conv={{ id: 7, title, updated_at: '2026-01-01T00:00:00Z' }}
      active={false}
      running={false}
      blocked={false}
      finished={null}
      menuOpen={false}
      setMenuOpen={() => {}}
      onOpen={() => {}}
      onExport={() => {}}
      onSys={() => {}}
      onDelete={() => {}}
    />,
  )

describe('ConversationRow provider warning slot (#255)', () => {
  it('shows an amber triangle with a handoff tooltip for provider-markup titles', () => {
    renderConv('<system_warning>⚠️ CONTEXT LOW - Prioritize completing current tasks</system_warning>')
    const marker = screen.getByTitle(/Provider signalled low context\. Run \/handoff/)
    expect(marker.className).toContain('text-amber-400')
    expect(marker.className).toContain('mr-1.5')
    expect(marker.className).toContain('shrink-0')
    // The raw provider text never renders as the visible title.
    expect(screen.queryByText(/CONTEXT LOW/)).toBeNull()
    expect(screen.getByText('New chat')).toBeTruthy()
  })

  it('renders no triangle for ordinary titles', () => {
    renderConv('Real chat title')
    expect(screen.queryByTitle(/Provider signalled low context/)).toBeNull()
  })
})
