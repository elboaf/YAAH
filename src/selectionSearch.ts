// #201: "Search on Google" on a chat-text selection. The selection reading
// and query construction live here as pure-ish helpers so the trigger
// condition and URL building are unit-testable without the full ChatPanel.

/** Cap on the ENCODED query length — browsers and Google reject very long
 *  URLs, so trailing partial content past this is dropped. */
export const SEARCH_QUERY_MAX_CHARS = 1500

/**
 * The selected text a right-click on the transcript should offer as a search,
 * or null when the menu must NOT appear: no selection, a collapsed (caret)
 * selection, or a selection whose anchor lies outside the transcript element.
 * Whitespace runs (including newlines from multi-line selections) collapse to
 * single spaces and the result is trimmed; an empty result means no menu.
 */
export function transcriptSelection(transcript: HTMLElement | null): string | null {
  const sel = window.getSelection()
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null
  if (!transcript || !sel.anchorNode || !transcript.contains(sel.anchorNode)) return null
  const text = sel.toString().replace(/\s+/g, ' ').trim()
  return text.length > 0 ? text : null
}

/**
 * The Google search URL for a selection text: URL-encoded, capped at
 * SEARCH_QUERY_MAX_CHARS of the ENCODED query (dropping trailing partial
 * content, never a partial percent-escape).
 */
export function googleSearchUrl(text: string): string {
  let encoded = encodeURIComponent(text)
  if (encoded.length > SEARCH_QUERY_MAX_CHARS) {
    encoded = encoded
      .slice(0, SEARCH_QUERY_MAX_CHARS)
      // Never end mid-escape: a trailing '%' or '%h' would corrupt the URL.
      .replace(/%(?:[0-9a-fA-F]{0,2})$/, (m) => (m.length === 3 ? m : ''))
  }
  return `https://www.google.com/search?q=${encoded}`
}
