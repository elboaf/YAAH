// #52: stick-to-bottom scrolling discipline for streaming transcripts.
//
// The reader, not the stream, owns the scroll position: streamed chunks only
// auto-scroll while the reader is parked at (near) the bottom, so scrolling
// up to re-read history during a run sticks until they scroll back down
// themselves. Within 50px of the bottom counts as "at bottom" so partial
// scrolling doesn't detach the pin.
//
// `force` overrides the pin for one effect pass — used to snap back to the
// newest message when input is required (a pending question / approval /
// plan) or when the run completed and the chat is static again.

import { useCallback, useEffect, useRef, type DependencyList } from 'react'

const NEAR_BOTTOM_PX = 50

export function useStickToBottom(force: boolean, deps: DependencyList) {
  const containerRef = useRef<HTMLDivElement>(null)
  // True while the reader is at (near) the bottom. A ref, not state: the pin
  // updates on every scroll event without re-rendering the transcript.
  const pinnedToBottom = useRef(true)

  // Snap to the newest content whenever deps fire (streamed deltas, message
  // reloads, mount) — but only while pinned, or when forced by an attention
  // event that requires the user's eyes.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    if (pinnedToBottom.current || force) el.scrollTop = el.scrollHeight
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, force])

  // Maintain the pin from real scroll events (programmatic scrolls included:
  // writing scrollTop fires onScroll, which re-derives the same pin).
  const onScroll = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    pinnedToBottom.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX
  }, [])

  return { containerRef, onScroll }
}
