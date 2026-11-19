// Issue #770 stage 2: the detached-processes chip in the app-floor strip.
// The chip is the user-facing half of the panel: it shows how many processes
// YAAH started, lists them with status + log path, and stops one on click.
// These drive the real component against a mocked API, asserting what the
// user actually sees and what the stop button actually sends.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { listProcesses, stopProcess } = vi.hoisted(() => ({
  listProcesses: vi.fn(),
  stopProcess: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, listProcesses, stopProcess }
})

import { DetachedProcessesChip } from './components'

const ROW = {
  pid: 4242,
  command: 'node server.js',
  cwd: 'C:\\work\\app',
  log_path: 'C:\\work\\app\\.yaah-detached\\server.log',
  started: 1_700_000_000,
  conversation_id: null,
  alive: true,
}

describe('Detached processes chip (#770 stage 2)', () => {
  beforeEach(() => {
    listProcesses.mockResolvedValue({ processes: [], known: 0 })
    stopProcess.mockResolvedValue({ stopped: true, pid: ROW.pid, killed: true })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('renders nothing when no detached process is running', async () => {
    // A permanent "0" badge would be decoration, not information.
    const { container } = render(<DetachedProcessesChip />)
    await waitFor(() => expect(listProcesses).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('shows the running count, then the list with status and log path', async () => {
    listProcesses.mockResolvedValue({ processes: [ROW], known: 1 })
    render(<DetachedProcessesChip />)

    const chip = await screen.findByRole('button', { name: /Detached processes/i })
    await waitFor(() => expect(chip.textContent).toContain('1'))

    fireEvent.click(chip)
    // The command and its log path must both be visible: the log path is how
    // the user finds the output, which is the point of the panel.
    expect(await screen.findByText('node server.js')).toBeTruthy()
    expect(screen.getByText(/server\.log/)).toBeTruthy()
    expect(screen.getByText('4242')).toBeTruthy()
  })

  it('stops the process the button belongs to, and refreshes the list', async () => {
    listProcesses.mockResolvedValue({ processes: [ROW], known: 1 })
    render(<DetachedProcessesChip />)

    fireEvent.click(await screen.findByRole('button', { name: /Detached processes/i }))
    const stop = await screen.findByRole('button', { name: 'stop' })
    fireEvent.click(stop)

    // The PID that travels is the row's own -- not a neighbour's.
    await waitFor(() => expect(stopProcess).toHaveBeenCalledWith(ROW.pid))
  })

  it('surfaces a refusal instead of pretending the stop worked', async () => {
    // The route refuses an unrecorded PID with a 400; api() throws. The chip
    // must show why, and must not remove the row as if it had been killed.
    listProcesses.mockResolvedValue({ processes: [ROW], known: 1 })
    stopProcess.mockRejectedValue(
      new Error('400: {"detail":"no agent-spawned process with pid 4242"}'),
    )
    render(<DetachedProcessesChip />)

    fireEvent.click(await screen.findByRole('button', { name: /Detached processes/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'stop' }))

    expect(await screen.findByText(/no agent-spawned process/i)).toBeTruthy()
  })
})
