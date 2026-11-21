// Issue #681: worker chats are first-class in the sidebar — a worker row
// renders the 'wkr' badge (same footprint slot as the agent silhouette),
// an ordinary row does not, and the row never shifts layout.

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import { ConversationRow } from './components'

const renderRow = (props: { isWorker?: boolean; isAgent?: boolean }) =>
  render(
    <ConversationRow
      conv={{ id: 7, title: 'Fixer', updated_at: '2026-01-01T00:00:00Z' }}
      active={false}
      running={false}
      blocked={false}
      finished={null}
      isAgent={props.isAgent}
      isWorker={props.isWorker}
      menuOpen={false}
      setMenuOpen={() => {}}
      onOpen={() => {}}
      onExport={() => {}}
      onSys={() => {}}
      onDelete={() => {}}
    />,
  )

describe('ConversationRow worker badge (#681)', () => {
  it('renders the wkr badge with the desk tooltip for a worker chat', () => {
    renderRow({ isWorker: true })
    const badge = screen.getByTitle(/Worker chat — runs at a desk/)
    expect(badge.textContent).toBe('wkr')
    expect(badge.className).toContain('mr-1.5')
    expect(badge.className).toContain('shrink-0')
  })

  it('renders no badge for an ordinary chat', () => {
    renderRow({})
    expect(screen.queryByTitle(/Worker chat — runs at a desk/)).toBeNull()
  })

  it('still shows the title', () => {
    renderRow({ isWorker: true })
    expect(screen.getByText('Fixer')).toBeTruthy()
  })
})
