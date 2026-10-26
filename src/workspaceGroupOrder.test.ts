import { describe, expect, it } from 'vitest'
import { sortWorkspaceGroups, type WorkspaceGroupOrder } from './workspaceGroupOrder'

const group = (
  path: string | null,
  last_opened_at: string | null,
): WorkspaceGroupOrder => ({
  ws: { path, last_opened_at },
})

describe('sortWorkspaceGroups (#91)', () => {
  it('orders Default first and other groups by their workspace activity', () => {
    const groups = [
      group('C:/b', '2026-09-22 10:00:00'),
      group(null, null),
      group('C:/a', '2026-09-22 11:00:00'),
    ]

    expect(sortWorkspaceGroups(groups).map((g) => g.ws.path)).toEqual([
      null,
      'C:/a',
      'C:/b',
    ])
  })

  it('does not reorder groups when background message writes change conversation activity', () => {
    const before = [
      group('C:/a', '2026-09-22 11:00:00'),
      group('C:/b', '2026-09-22 10:00:00'),
    ]
    const afterBackgroundWrite = [
      group('C:/a', '2026-09-22 11:00:00'),
      group('C:/b', '2026-09-22 10:00:00'),
    ]

    expect(sortWorkspaceGroups(afterBackgroundWrite).map((g) => g.ws.path)).toEqual(
      sortWorkspaceGroups(before).map((g) => g.ws.path),
    )
  })

  it('moves a group when the workspace is deliberately touched', () => {
    const before = [
      group('C:/a', '2026-09-22 11:00:00'),
      group('C:/b', '2026-09-22 10:00:00'),
    ]
    const afterTouch = [
      group('C:/a', '2026-09-22 11:00:00'),
      group('C:/b', '2026-09-22 12:00:00'),
    ]

    expect(sortWorkspaceGroups(afterTouch).map((g) => g.ws.path)).toEqual(['C:/b', 'C:/a'])
  })

  it('#42: manual position fully replaces the activity sort once any workspace is placed', () => {
    const groups = [
      group('C:/b', '2026-09-23 10:00:00'),
      group('C:/a', '2026-09-23 11:00:00'),
      group('C:/c', null),
    ]
    // C:/a was dragged to the top (position 0); everything else follows in
    // placed-then-never-placed order, ignoring last_opened_at entirely.
    const withPositions = groups.map((g, i) =>
      i === 1 ? { ...g, ws: { ...g.ws, position: 0 } } : g,
    )

    expect(sortWorkspaceGroups(withPositions).map((g) => g.ws.path)).toEqual([
      'C:/a',
      'C:/b',
      'C:/c',
    ])
  })

  it('#42: Default stays first even under manual order', () => {
    const groups = [
      group('C:/a', '2026-09-23 11:00:00'),
      group(null, null),
      group('C:/b', null),
    ]
    const withPositions = groups.map((g, i) =>
      i === 0 ? { ...g, ws: { ...g.ws, position: 0 } } : g,
    )

    expect(sortWorkspaceGroups(withPositions).map((g) => g.ws.path)).toEqual([
      null,
      'C:/a',
      'C:/b',
    ])
  })
})
