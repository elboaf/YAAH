/** A sidebar workspace group, narrowed to the fields used for ordering. */
export interface WorkspaceGroupOrder {
  ws: { path: string | null; last_opened_at: string | null; position?: number | null }
}

/** Keep Default first; sort other groups by deliberate workspace activity.
 * Conversation timestamps change on every streamed message, so they are not
 * suitable for ordering workspace groups.
 *
 * #42: once the user has dragged ANY workspace, manual order fully replaces
 * the activity sort — a hybrid ("position wins when both set, else recency")
 * lets one unordered row flip a group back into the churning activity sort
 * (the #91 bug). Default always stays first. */
export function sortWorkspaceGroups<T extends WorkspaceGroupOrder>(groups: T[]): T[] {
  const manuallyOrdered = groups.some((g) => (g.ws.path !== null && g.ws.position != null))
  return [...groups].sort((a, b) => {
    const aIsDefault = a.ws.path === null
    const bIsDefault = b.ws.path === null
    if (aIsDefault || bIsDefault) return Number(bIsDefault) - Number(aIsDefault)
    if (manuallyOrdered) {
      // NULLs last: never-dragged workspaces trail the placed ones.
      const aPos = a.ws.position ?? Number.MAX_SAFE_INTEGER
      const bPos = b.ws.position ?? Number.MAX_SAFE_INTEGER
      if (aPos !== bPos) return aPos - bPos
      return 0
    }
    return (b.ws.last_opened_at ?? '').localeCompare(a.ws.last_opened_at ?? '')
  })
}
