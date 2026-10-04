import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const { getRunWorktrees } = vi.hoisted(() => ({
  getRunWorktrees: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    getRunWorktrees,
  }
})

import { GitChipCluster } from './components'
import type { GitInfo, RunWorktree } from './api'

// #290: a separate badge in the chip cluster shows run-in-flight state,
// POLLED from git (never agent announcements), and the selector chip —
// the user's pick (#286's whole point) — is never recolored for it.
function info(overrides: Partial<GitInfo> = {}): GitInfo {
  return {
    branch: 'master',
    upstream: null,
    local_hash: 'abc1234',
    remote_hash: null,
    ahead: 0,
    behind: 0,
    added: 0,
    deleted: 0,
    dirty: false,
    untracked: 0,
    changed: 0,
    ...overrides,
  }
}

function run(overrides: Partial<RunWorktree> = {}): RunWorktree {
  return {
    branch: 'run/chat-7',
    chat_id: '7',
    leaf: 'run',
    path: 'C:/ws/.scratch/chat-7/run',
    dirty: false,
    merged: false,
    residue: 'unmerged',
    ...overrides,
  }
}

function mountCluster(opts: {
  info: GitInfo
  selectedBranch: string | null
  runs?: RunWorktree[]
}) {
  getRunWorktrees.mockResolvedValue({ runs: opts.runs ?? [], target: 'master' })
  return render(
    <GitChipCluster
      info={opts.info}
      streaming={false}
      conversationId={7}
      selectedBranch={opts.selectedBranch}
      onCommandDone={() => {}}
    />,
  )
}

describe('run-in-flight badge (#290)', () => {
  beforeEach(() => {
    getRunWorktrees.mockResolvedValue({ runs: [], target: 'master' })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('lights when a run worktree exists, naming branch and target', async () => {
    mountCluster({ info: info(), selectedBranch: null, runs: [run()] })
    const badge = await screen.findByRole('status', { name: /run in flight/i })
    expect(badge.textContent).toContain('run/chat-7')
    expect(badge.textContent).toContain('master')
  })

  it('attributes the run to its own chat — other chats’ runs stay dark', async () => {
    mountCluster({ info: info(), selectedBranch: null, runs: [run({ chat_id: '9' })] })
    await screen.findByRole('button', { name: /branch for this chat/i })
    expect(screen.queryByRole('status', { name: /run in flight/i })).toBeNull()
  })

  it('is dark with no run worktrees', async () => {
    mountCluster({ info: info(), selectedBranch: null, runs: [] })
    await screen.findByRole('button', { name: /branch for this chat/i })
    expect(screen.queryByRole('status', { name: /run in flight/i })).toBeNull()
  })

  it('polls — state survives reload because the backend derives it', async () => {
    mountCluster({ info: info(), selectedBranch: null, runs: [run()] })
    await screen.findByRole('status', { name: /run in flight/i })
    expect(getRunWorktrees).toHaveBeenCalledWith(7)
  })

  it('marks residue in the tooltip: dirty and unmerged surface, clean says removable', async () => {
    const { container } = mountCluster({
      info: info(),
      selectedBranch: null,
      runs: [run({ residue: 'dirty', dirty: true })],
    })
    const badge = await screen.findByRole('status', { name: /run in flight/i })
    expect(badge.getAttribute('title')).toMatch(/uncommitted/i)
    expect(badge.getAttribute('title')).toMatch(/land it|scrap it/i)

    cleanup()
    getRunWorktrees.mockResolvedValue({
      runs: [run({ residue: 'clean', merged: true })],
      target: 'master',
    })
    const second = render(
      <GitChipCluster
        info={info()}
        streaming={false}
        conversationId={7}
        selectedBranch={null}
        onCommandDone={() => {}}
      />,
    )
    const cleanBadge = await screen.findByRole('status', { name: /run in flight/i })
    expect(cleanBadge.getAttribute('title')).toMatch(/removable|landed/i)
    container.remove()
    second.unmount()
  })

  it('the selector chip keeps its meaning — no run-state recoloring', async () => {
    mountCluster({ info: info(), selectedBranch: 'bigtest', runs: [run()] })
    const selector = await screen.findByRole('button', { name: /branch for this chat/i })
    // The dirty-state dot is driven by workspace dirty only, not runs.
    expect(selector.className).not.toMatch(/sky|blue/)
    expect(selector.textContent).not.toContain('run/chat-7')
  })

  it('renders nothing without a conversation', () => {
    getRunWorktrees.mockResolvedValue({ runs: [], target: null })
    render(
      <GitChipCluster
        info={info()}
        streaming={false}
        conversationId={null}
        selectedBranch={null}
        onCommandDone={() => {}}
      />,
    )
    expect(screen.queryByRole('status', { name: /run in flight/i })).toBeNull()
    expect(getRunWorktrees).not.toHaveBeenCalled()
  })
})
