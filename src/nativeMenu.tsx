import { useEffect } from 'react'

/**
 * #287: the WebView2 default right-click menu (Back / Refresh / Save as /
 * Print) is browser chrome, not Yaah UI — a desktop agent has no history to
 * go Back to and nothing to Print. One document-level capture-phase listener
 * suppresses it app-wide EXCEPT where its items are the right desktop
 * affordances:
 *
 *  - editable fields (input / textarea / contenteditable): Undo, Cut, Copy,
 *    Paste live in the native menu and re-implementing Paste against
 *    WebView2's clipboard is not worth it;
 *  - images: Save image as / Copy image are congruent.
 *
 * Capture phase on document so the decision is made before any app handler
 * needs to care. The transcript's React handler (#276) still runs afterward
 * (capture here does not stop it): a qualifying selection opens the custom
 * Copy + Search on Google menu, everything else just stays suppressed.
 * Links in chat markdown are deliberately in the suppress bucket — clicks
 * already route through openExternal; a Copy-link item would be a future
 * custom-menu addition.
 *
 * Test-only unmount: this is app-shell-lifetime state, so the component
 * never re-renders; the cleanup path exists for the suite.
 */
export function NativeMenuGate(): null {
  useEffect(() => {
    const onContextMenu = (e: MouseEvent) => {
      const target = e.target
      if (!(target instanceof Element)) return
      if (target.closest('input, textarea, [contenteditable], img')) return
      e.preventDefault()
    }
    document.addEventListener('contextmenu', onContextMenu, true)
    return () => document.removeEventListener('contextmenu', onContextMenu, true)
  }, [])
  return null
}
