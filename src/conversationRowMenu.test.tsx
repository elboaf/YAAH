// Issue #127: the conversation row "…" menu had two usability gaps.
// (1) Its bg-zinc-900 background blended into the sidebar chrome behind it,
//     so the open menu did not stand out — it must carry a visually
//     distinguishing surface (lighter background AND a border, per the app's
//     dark palette) plus the existing shadow.
// (2) The menu closed only via the toggle button, an item click, or Escape;
//     clicking elsewhere left it floating over content — a document-level
//     pointerdown outside the menu (and its toggle) must close it.
// Escape dismissal lives at the sidebar level (components.tsx, menuOpenId
// effect) and is unchanged by this feature.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { ConversationRow } from './components'

const baseConv = { id: 7, title: 'Chat seven', updated_at: '2026-01-01T00:00:00Z' }

const renderRow = (menuOpen: boolean, setMenuOpen = vi.fn()) => {
  render(
    <ConversationRow
      conv={baseConv}
      active={false}
      running={false}
      blocked={false}
      finished={null}
      menuOpen={menuOpen}
      setMenuOpen={setMenuOpen}
      onOpen={() => {}}
      onExport={() => {}}
      onSys={() => {}}
      onDelete={() => {}}
    />,
  )
  return setMenuOpen
}

const openMenu = () => {
  const setMenuOpen = vi.fn()
  renderRow(true, setMenuOpen)
  return setMenuOpen
}

afterEach(() => cleanup())

describe('conversation row "…" menu (#127)', () => {
  it('open menu stands out from the sidebar: lighter background and a border', () => {
    openMenu()
    const menu = screen.getByRole('menu')
    expect(menu.className).toContain('bg-zinc-800')
    expect(menu.className).toContain('border')
    expect(menu.className).not.toContain('bg-zinc-900')
  })

  it('pointerdown outside the menu closes it', () => {
    const setMenuOpen = openMenu()
    fireEvent.pointerDown(document, { target: document.body })
    expect(setMenuOpen).toHaveBeenCalledWith(false)
  })

  it('pointerdown inside the menu or on its toggle button does not close it', () => {
    const setMenuOpen = openMenu()
    fireEvent.pointerDown(screen.getByRole('menu'))
    expect(setMenuOpen).not.toHaveBeenCalled()
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Conversation actions' }))
    expect(setMenuOpen).not.toHaveBeenCalled()
  })

  it('clicking a menu item still performs the action and closes the menu', () => {
    const onExport = vi.fn()
    const setMenuOpen = vi.fn()
    render(
      <ConversationRow
        conv={baseConv}
        active={false}
        running={false}
        blocked={false}
        finished={null}
        menuOpen
        setMenuOpen={setMenuOpen}
        onOpen={() => {}}
        onExport={onExport}
        onSys={() => {}}
        onDelete={() => {}}
      />,
    )
    fireEvent.click(screen.getByText('Export as Markdown'))
    expect(onExport).toHaveBeenCalledTimes(1)
    expect(setMenuOpen).toHaveBeenCalledWith(false)
  })

  it('the "…" button still toggles via setMenuOpen', () => {
    const setMenuOpen = renderRow(false)
    fireEvent.click(screen.getByRole('button', { name: 'Conversation actions' }))
    expect(setMenuOpen).toHaveBeenCalledWith(true)
  })

  it('menu is presented as expanded to the toggle while open (Escape dismissal lives in the sidebar, guarded there)', () => {
    // The Escape listener lives in the sidebar component; guard the
    // component-level contract by asserting the menu is open to begin with
    // and that the toggle reflects expanded state (aria-expanded).
    openMenu()
    expect(
      screen.getByRole('button', { name: 'Conversation actions' }).getAttribute('aria-expanded'),
    ).toBe('true')
  })
})
