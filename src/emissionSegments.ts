// #275: stable emission segmentation for the live transcript.
//
// The old renderer keyed emission segments by their running cursor offset
// (`agent-seg-${cursor}`) and — worse — only built segments once the first
// anchor (answered ask_user / spawn_agent) existed: a plain streaming
// message rendered through a DIFFERENT branch, and the first anchor's
// arrival replaced the whole body DOM, destroying any in-progress text
// selection. React keys on shifting offsets also re-pair content under
// existing keys when anchors interleave.
//
// The fix: one segmentation for every live agent message, keyed by where
// each segment's text STARTS (the running cursor at emission time; the
// final growing tail is keyed by the offset it starts at). A new anchor
// inserted mid-stream adds new keys around the insertion point and never
// moves an existing segment's start offset; every existing segment keeps
// its key and its DOM node — and with it, the reader's selection.

import type { ReactNode } from 'react'
import type { ChatMessage, ToolCall } from './store'

export interface EmissionSegment {
  /** React key: stable across mid-stream boundary insertions. */
  key: string
  /** Emission text between the previous boundary and this segment's anchor. */
  content: string
  /** The tool call rendered right after this segment's text, if any. */
  anchor?: ToolCall
}

/**
 * Split a (possibly still-streaming) agent message into emission segments.
 * Anchors are the message's answered ask_user calls and spawn_agent runs
 * that carry a contentOffset; each is placed at its recorded offset, and
 * every TEXT segment — including the growing tail after the last anchor —
 * is keyed by the cursor offset where its text STARTS
 * (`seg-${msg.id}-${cursor}`). Cursor offsets advance only when text is
 * emitted, so keys are unique; and because an anchor's arrival only ever
 * SPLITS the tail (an equal-offset anchor emits no text segment), every
 * existing segment keeps its starting offset — and therefore its key and
 * its DOM node — no matter where new boundaries land.
 *
 * Legacy rows whose answered asks lack offsets return null (the caller
 * falls back to the chronological/flat render).
 */
export function emissionSegments(msg: ChatMessage): EmissionSegment[] | null {
  const calls = msg.toolCalls ?? []
  const answered = calls.filter((t) => t.name === 'ask_user' && t.result !== undefined)
  // Legacy shape: answered asks exist but at least one has no recorded
  // offset — the caller's chronological/flat fallback handles those.
  if (answered.length > 0 && answered.some((t) => typeof t.contentOffset !== 'number')) {
    return null
  }
  const anchors = calls
    .filter(
      (t) =>
        ((t.name === 'ask_user' && t.result !== undefined) ||
          t.name === 'spawn_agent') &&
        typeof t.contentOffset === 'number',
    )
    .sort((a, b) => (a.contentOffset ?? 0) - (b.contentOffset ?? 0))
  const segments: EmissionSegment[] = []
  let cursor = 0
  for (const anchor of anchors) {
    const cut = Math.min(
      Math.max(anchor.contentOffset ?? 0, cursor),
      msg.content.length,
    )
    if (cut > cursor) {
      segments.push({
        key: `seg-${msg.id}-${cursor}`,
        content: msg.content.slice(cursor, cut),
      })
      cursor = cut
    }
    segments.push({ key: `anchor-${anchor.id}`, content: '', anchor })
  }
  if (cursor < msg.content.length || segments.length === 0) {
    segments.push({ key: `seg-${msg.id}-${cursor}`, content: msg.content.slice(cursor) })
  }
  return segments
}

/** Render helper type shim: the caller maps anchors to nodes. */
export type SegmentAnchorRenderer = (anchor: ToolCall) => ReactNode
