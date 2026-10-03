// Issue #243 — the AgentRunWatcher must only toast "run failed" for genuine
// failures. A run that settles `last_status: "ok"` (now including runs whose
// only error event was non-fatal, e.g. a claim refusal) never toasts error;
// `error_quiet` (rate limits) stays silent too; `error` still toasts.
// Run: npx vitest run src/agentRunWatcherFalseToast.test.tsx
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, act } from '@testing-library/react'
import { render } from '@testing-library/react'
import { AgentRunWatcher } from './components'
import { useAgent } from './store'

afterEach(() => {
  cleanup()
  useAgent.setState({ agents: [], agentsToastedThrough: {}, toasts: [] })
  vi.restoreAllMocks()
})

type AgentStatus = 'ok' | 'error' | 'error_quiet' | 'running'

function prime(status: AgentStatus, first = true) {
  const agent = {
    id: 'a1',
    name: 'nightly',
    conversation_id: 7,
    running: false,
    enabled: 1,
    last_status: status,
    last_finished_at: '2026-10-02 12:00:00',
    notify_on_success: false,
  } as unknown as import('./api').ScheduledAgent
  act(() => {
    useAgent.setState({
      agents: [agent],
      agentsToastedThrough: first ? {} : { a1: '2026-10-02 11:00:00' },
    })
  })
}

function watchOnce() {
  // The watcher's effect polls once on mount.
  render(<AgentRunWatcher />)
}

describe('AgentRunWatcher false "run failed" toast (#243)', () => {
  it('completed run (ok) never toasts error', async () => {
    prime('error_quiet') // toast-seen baseline from a previous settle
    act(() => {
      useAgent.setState({
        agentsToastedThrough: { a1: '2026-10-02 11:00:00' },
        agents: [{
          id: 'a1', name: 'nightly', conversation_id: 7, running: false,
          enabled: 1, last_status: 'ok',
          last_finished_at: '2026-10-02 12:00:00', notify_on_success: false,
        } as unknown as import('./api').ScheduledAgent],
      })
    })
    watchOnce()
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
    expect(useAgent.getState().toasts).toEqual([])
  })

  it('rate-limit quiet settle never toasts', async () => {
    act(() => {
      useAgent.setState({
        agentsToastedThrough: { a1: '2026-10-02 11:00:00' },
        agents: [{
          id: 'a1', name: 'nightly', conversation_id: 7, running: false,
          enabled: 1, last_status: 'error_quiet',
          last_finished_at: '2026-10-02 12:00:00', notify_on_success: false,
        } as unknown as import('./api').ScheduledAgent],
      })
    })
    watchOnce()
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
    expect(useAgent.getState().toasts).toEqual([])
  })

  it('genuine failure (error) still toasts', async () => {
    act(() => {
      useAgent.setState({
        agentsToastedThrough: { a1: '2026-10-02 11:00:00' },
        agents: [{
          id: 'a1', name: 'nightly', conversation_id: 7, running: false,
          enabled: 1, last_status: 'error',
          last_finished_at: '2026-10-02 12:00:00', notify_on_success: false,
        } as unknown as import('./api').ScheduledAgent],
      })
    })
    watchOnce()
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
    const toasts = useAgent.getState().toasts
    expect(toasts).toHaveLength(1)
    expect(toasts[0].kind).toBe('error')
    expect(toasts[0].title).toMatch(/run failed/)
  })
})
