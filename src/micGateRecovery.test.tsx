// Regression: the composer's dictation button must come back when the
// backend recovers late — without depending on catching a one-shot event
// or on a full-page reload.
//
// User report (post-update session, 2026-10-04): the mic button in the
// composer's bottom-right toolbar was missing for the whole session after
// an update. The gate (components.tsx) polls /api/transcribe/status on
// mount with bounded retries (~54s total), then gives up silently; the
// only re-check trigger is a 'backend-status' event, which the supervisor
// emits exactly once per spawn — BEFORE the webview has listeners
// attached (win.eval into a still-loading page). A lost 'up' event plus
// exhausted retries = a button hidden until the user happens to trigger
// some other full-page reload. These tests pin the recovery contract:
//
//   1. a late-but-successful status probe renders the button (existing
//      retry behavior, kept),
//   2. even after the retry budget is exhausted, the gate keeps polling
//      slowly and renders the button as soon as the backend answers
//      healthy — no event required,
//   3. the 'backend-status' wakeup still forces an immediate re-check.
//
// Run: npx vitest run src/micGateRecovery.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

const { transcribeStatus } = vi.hoisted(() => ({
  transcribeStatus: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    transcribeStatus,
  }
})

import { Composer } from './components'
import { useAgent } from './store'

const STATUS = {
  engine: 'local' as const,
  local_available: true,
  local_model: 'ggml-base-q5_1.bin',
  cloud_configured: false,
}

beforeEach(() => {
  transcribeStatus.mockReset()
  useAgent.setState({
    conversationId: null,
    statusByConv: {},
    messagesByConv: {},
    pendingQuestions: {},
    pendingApprovals: {},
    pendingPlanApprovals: {},
  })
})

afterEach(() => {
  cleanup()
})

describe('mic button gate recovery', () => {
  it('renders the mic button when the late status probe finally succeeds', async () => {
    // Simulate the update-morning backend: down for the first attempts
    // (mount burst), then healthy — well inside the retry budget, so this
    // half of the gate already worked and must keep working.
    transcribeStatus
      .mockRejectedValueOnce(new Error('Backend is unreachable (restarting)'))
      .mockRejectedValueOnce(new Error('Backend is unreachable (restarting)'))
      .mockResolvedValue(STATUS)

    render(<Composer />)

    await waitFor(
      () => {
        expect(screen.getByRole('button', { name: 'Dictate (voice to text)' })).toBeInTheDocument()
      },
      // The first retries back off 1.5s/3s — longer than the default 1s
      // timeout, so give the probe chain room.
      { timeout: 6000 },
    )
  })

  it('keeps polling slowly after the retry budget and recovers without any event', async () => {
    vi.useFakeTimers()
    try {
      // Down for the entire bounded retry window: the gate exhausts all 9
      // attempts — the session-long hole the user reported.
      transcribeStatus.mockRejectedValue(new Error('Backend is unreachable (restarting)'))

      render(<Composer />)
      expect(screen.queryByRole('button', { name: 'Dictate (voice to text)' })).toBeNull()

      // Burn the whole retry schedule: 9 attempts backing off 1.5s..13.5s.
      await vi.advanceTimersByTimeAsync(60_000)
      expect(transcribeStatus.mock.calls.length).toBeGreaterThanOrEqual(9)
      expect(screen.queryByRole('button', { name: 'Dictate (voice to text)' })).toBeNull()

      // Minutes later (long past the old give-up), the backend comes back.
      // NO backend-status event is fired — the gate itself must notice.
      transcribeStatus.mockResolvedValue(STATUS)
      await vi.advanceTimersByTimeAsync(180_000)
      // The healthy answer flipped state; the re-render lands on the next
      // scheduler tick — yield once more before asserting.
      await vi.advanceTimersByTimeAsync(0)
      expect(screen.getByRole('button', { name: 'Dictate (voice to text)' })).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('re-checks immediately when a backend-status event arrives', async () => {
    vi.useFakeTimers()
    try {
      transcribeStatus.mockRejectedValue(new Error('Backend is unreachable (restarting)'))
      render(<Composer />)

      // The supervisor's 'up' event must force a fresh probe even though
      // the mount-time chain is mid-backoff.
      window.dispatchEvent(new CustomEvent('backend-status', { detail: { status: 'up' } }))
      await vi.advanceTimersByTimeAsync(0)
      expect(transcribeStatus.mock.calls.length).toBeGreaterThanOrEqual(2)

      // The event-fired probe raced our mock flip (it saw the down backend),
      // so let its retry land inside the normal backoff — well under the old
      // give-up budget, and with no further events.
      transcribeStatus.mockResolvedValue(STATUS)
      await vi.advanceTimersByTimeAsync(5_000)
      expect(screen.getByRole('button', { name: 'Dictate (voice to text)' })).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})
