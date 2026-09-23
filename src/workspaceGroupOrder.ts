/** A sidebar workspace group, narrowed to the fields used for ordering. */
export interface WorkspaceGroupOrder {
  ws: { path: string | null; last_opened_at: string | null }
  items: readonly { updated_at: string }[]
}

/** Keep Default first; sort other groups by deliberate workspace activity.
 * Conversation timestamps change on every streamed message, so they are not
 * suitable for ordering workspace groups. */
export function sortWorkspaceGroups<T extends WorkspaceGroupOrder>(groups: T[]): T[] {
  return [...groups].sort((a, b) => {
    const aIsDefault = a.ws.path === null
    const bIsDefault = b.ws.path === null
    if (aIsDefault || bIsDefault) return Number(bIsDefault) - Number(aIsDefault)
    return (b.ws.last_opened_at ?? '').localeCompare(a.ws.last_opened_at ?? '')
  })
}
