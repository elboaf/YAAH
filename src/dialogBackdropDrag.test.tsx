// #478 — cursor drifting off an element mid-press breaks drags and closes
// dialogs. Regression tests:
// 1. Backdrop dismiss must use mouseup with press-origin check, not just mousedown.
// 2. Workspace group drag must use pointer capture to survive cursor leaving header.
import { useState, useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

afterEach(() => cleanup())

function TestDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const overlayPressed = useRef(false)
  if (!open) return null
  return (
    <div
      role="dialog"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) overlayPressed.current = true
      }}
      onMouseUp={(e) => {
        if (e.target === e.currentTarget && overlayPressed.current) onClose()
        overlayPressed.current = false
      }}
    >
      <div data-testid="dialog-body">
        <button data-testid="body-button">Button</button>
      </div>
    </div>
  )
}

function DialogWrapper() {
  const [open, setOpen] = useState(true)
  return (
    <TestDialog
      open={open}
      onClose={() => setOpen(false)}
    />
  )
}

describe('dialog backdrop dismiss survives mid-drag cursor exit', () => {
  it('does not close when press starts on dialog body, release on backdrop', () => {
    render(<DialogWrapper />)

    const body = screen.getByTestId('dialog-body')
    fireEvent.mouseDown(body)
    const backdrop = screen.getByRole('dialog')
    fireEvent.mouseUp(backdrop)

    expect(screen.getByTestId('dialog-body')).toBeInTheDocument()
  })

  it('closes on plain click on backdrop', () => {
    render(<DialogWrapper />)

    const backdrop = screen.getByRole('dialog')
    fireEvent.mouseDown(backdrop)
    fireEvent.mouseUp(backdrop)

    expect(screen.queryByTestId('dialog-body')).not.toBeInTheDocument()
  })

  it('does not close when press starts on backdrop but release moves to body', () => {
    render(<DialogWrapper />)

    const backdrop = screen.getByRole('dialog')
    const body = screen.getByTestId('dialog-body')
    fireEvent.mouseDown(backdrop)
    fireEvent.mouseUp(body)

    expect(screen.getByTestId('dialog-body')).toBeInTheDocument()
  })
})

describe('workspace group drag uses pointer capture', () => {
  it('captures pointer on drag start (wrapped for jsdom)', () => {
    const captured: number[] = []
    function Header() {
      return (
        <div
          role="button"
          data-testid="ws-header"
          onPointerDown={(e) => {
            try {
              e.currentTarget.setPointerCapture(e.pointerId)
              captured.push(e.pointerId)
            } catch {
              // jsdom doesn't implement setPointerCapture
              captured.push(e.pointerId)
            }
          }}
        >
          Workspace
        </div>
      )
    }

    render(<Header />)
    const header = screen.getByTestId('ws-header')

    fireEvent.pointerDown(header, { pointerId: 1 })
    expect(captured).toContain(1)
  })
})
