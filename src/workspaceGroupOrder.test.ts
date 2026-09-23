import { describe, expect, it } from 'vitest'
import { sortWorkspaceGroups, type WorkspaceGroupOrder } from './workspaceGroupOrder'

const group = (
  path: string | null,
  last_opened_at: string | null,
  updated_at: string,
): WorkspaceGroupOrder => ({
  ws: { path, last_opened_at },
  items: [{ updated_at }],
})

describe('sortWorkspaceGroups (#91)', () => {
  it('orders Default first and other groups by their workspace activity', () => {
    const groups = [
      group('C:/b', '2026-09-22 10:00:00', '2026-09-22 12:00:00'),
      group(null, null, '2026-09-22 09:00:00'),
      group('C:/a', '2026-09-22 11:00:00', '2026-09-22 11:00:00'),
    ]

    expect(sortWorkspaceGroups(groups).map((g) => g.ws.path)).toEqual([
      null,
      'C:/a',
      'C:/b',
    ])
  })

  it('does not reorder groups when background message writes change conversation activity', () => {
    const before = [
      group('C:/a', '2026-09-22 11:00:00', '2026-09-22 11:00:00'),
      group('C:/b', '2026-09-22 10:00:00', '2026-09-22 10:00:00'),
    ]
    const afterBackgroundWrite = [
      group('C:/a', '2026-09-22 11:00:00', '2026-09-22 11:00:00'),
      group('C:/b', '2026-09-22 10:00:00', '2026-09-22 12:00:00'),
    ]

    expect(sortWorkspaceGroups(afterBackgroundWrite).map((g) => g.ws.path)).toEqual(
      sortWorkspaceGroups(before).map((g) => g.ws.path),
    )
  })

  it('moves a group when the workspace is deliberately touched', () => {
    const before = [
      group('C:/a', '2026-09-22 11:00:00', '2026-09-22 11:00:00'),
      group('C:/b', '2026-09-22 10:00:00', '2026-09-22 10:00:00'),
    ]
    const afterTouch = [
      group('C:/a', '2026-09-22 11:00:00', '2026-09-22 11:00:00'),
      group('C:/b', '2026-09-22 12:00:00', '2026-09-22 10:00:00'),
    ]

    expect(sortWorkspaceGroups(afterTouch).map((g) => g.ws.path)).toEqual(['C:/b', 'C:/a'])
  })
})
