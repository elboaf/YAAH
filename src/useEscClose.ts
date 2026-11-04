import { useEffect } from 'react'

/**
 * Closes a UI element (dialog, menu, lightbox) when the user presses Escape.
 * Only registers the listener while `open` is true; removes it on close/unmount.
 */
export function useEscClose(open: boolean, onClose: () => void): void {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])
}
