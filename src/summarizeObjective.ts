/**
 * #893: the goal overlay's summary used to recite the user's prompt verbatim —
 * a whole paragraph of text for what should be a "what's the goal?" reminder.
 *
 * This is a DISPLAY-ONLY transform: it runs at render time inside the two goal
 * surfaces (GoalOverlay, ObjectiveCard) and never touches the store. The
 * verbatim prompt stays exactly as-is in `messagesByConv` (the agent's source
 * of truth) and in `objectiveByConv`; the backend's `session_goal` is never
 * read here. Summarizing the overlay text has zero effect on what the agent
 * sees.
 *
 * Deterministic, no model call — the summary is decorative, so a model
 * dependency (latency, availability, out-of-the-box behavior on a fresh
 * workspace) would be a layer for no gain. The transform collapses whitespace
 * and caps the text at a word boundary, so a long prompt reads as a short line
 * instead of a wall of text.
 */

/** Character budget for the summarized line. A prompt within it renders
 *  unchanged (apart from whitespace collapse), so short goals are never
 *  truncated. */
export const OBJECTIVE_SUMMARY_LIMIT = 90

/** Whitespace-collapse the objective and, if it still exceeds the budget, cut
 *  it at the last word boundary before the limit and append an ellipsis.
 *  Returns the (possibly) shortened display string. Empty/whitespace input
 *  returns ''. */
export function summarizeObjective(text: string): string {
  if (!text || typeof text !== 'string') return ''
  // Collapse runs of whitespace (including newlines) to single spaces and
  // trim the ends — the prompt was written as free text, not as display copy.
  const collapsed = text.replace(/\s+/g, ' ').trim()
  if (collapsed.length <= OBJECTIVE_SUMMARY_LIMIT) return collapsed
  // Cut at the last space strictly before the limit so a word is never split
  // mid-letter; the ellipsis marks that more of the prompt follows.
  const cut = collapsed.slice(0, OBJECTIVE_SUMMARY_LIMIT)
  const lastSpace = cut.lastIndexOf(' ')
  const head = (lastSpace > 0 ? cut.slice(0, lastSpace) : cut).trimEnd()
  return `${head}…`
}
