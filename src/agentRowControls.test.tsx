// Sidebar row controls for scheduled agents (#199): three explicit
// controls instead of one ambiguous play/stop glyph —
//   bolt = run once now (dialog-identical run-now when enabled; a paused
//          agent runs once and stays paused),
//   play = resume the schedule (fires nothing),
//   stop = cancel the in-flight run, or disable the schedule of an idle agent.
// Paused rows must be visually unmistakable.
// Run: npx vitest run src/agentRowControls.test.tsx
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { ConversationRow } from './components'

afterEach(() => {
  cleanup()
})

const baseConv = { id: 1, title: 'nightly', updated_at: '2026-09-30 10:00' }

function renderRow(props: {
  running?: boolean
  agentEnabled?: boolean
  stopping?: boolean
  onRunOnce?: () => void
  onStopRun?: () => void
  onToggleEnable?: () => void
}) {
  return render(
    <ConversationRow
      conv={baseConv}
      active={false}
      running={props.running ?? false}
      blocked={false}
      finished={null}
      isAgent
      stopping={props.stopping ?? false}
      menuOpen={false}
      setMenuOpen={() => {}}
      onOpen={() => {}}
      onExport={() => {}}
      onSys={() => {}}
      onDelete={() => {}}
      onRunOnce={props.onRunOnce}
      onStopRun={props.onStopRun}
      onToggleEnable={props.onToggleEnable}
      agentEnabled={props.agentEnabled}
    />,
  )
}

describe('agent row controls (#199)', () => {
  it('paused + idle: bolt (one-shot) and play (resume), no stop; play fires nothing', () => {
    const onRunOnce = vi.fn()
    const onToggleEnable = vi.fn()
    const { container } = renderRow({ agentEnabled: false, onRunOnce, onToggleEnable })

    const bolt = screen.getByLabelText('Run once now')
    expect(bolt).toBeTruthy()
    expect(bolt.getAttribute('title')).toMatch(/paused schedule is unchanged/i)
    fireEvent.click(bolt)
    expect(onRunOnce).toHaveBeenCalledTimes(1)

    const play = screen.getByLabelText('Resume schedule')
    expect(play).toBeTruthy()
    // Paused must be unmistakable: the row itself says so.
    expect(container.textContent).toContain('paused')
    expect(screen.queryByLabelText('Stop run and pause schedule')).toBeNull()

    fireEvent.click(play)
    expect(onToggleEnable).toHaveBeenCalledTimes(1)
    expect(onRunOnce).toHaveBeenCalledTimes(1) // play never fires
  })

  it('enabled + idle: bolt (run now) and stop (disable the schedule)', () => {
    const onRunOnce = vi.fn()
    const onToggleEnable = vi.fn()
    renderRow({ agentEnabled: true, onRunOnce, onToggleEnable, onStopRun: onToggleEnable })

    const bolt = screen.getByLabelText('Run once now')
    expect(bolt.getAttribute('title')).toMatch(/run now/i)
    fireEvent.click(bolt)
    expect(onRunOnce).toHaveBeenCalledTimes(1)

    const stop = screen.getByLabelText('Stop run and pause schedule')
    fireEvent.click(stop)
    expect(onToggleEnable).toHaveBeenCalledTimes(1)
    expect(screen.queryByLabelText('Resume schedule')).toBeNull()
  })

  it('running: stop cancels the run, bolt is disabled, play is hidden', () => {
    const onStopRun = vi.fn()
    const onToggleEnable = vi.fn()
    renderRow({ running: true, agentEnabled: true, onStopRun, onToggleEnable, onRunOnce: () => {} })

    const stop = screen.getByLabelText('Stop run and pause schedule')
    fireEvent.click(stop)
    expect(onStopRun).toHaveBeenCalledTimes(1)

    const bolt = screen.getByLabelText('Run once now') as HTMLButtonElement
    expect(bolt.disabled).toBe(true)
    expect(onToggleEnable).not.toHaveBeenCalled()
    expect(screen.queryByLabelText('Resume schedule')).toBeNull()
  })

  it('non-agent conversations render no agent control cluster', () => {
    render(
      <ConversationRow
        conv={baseConv}
        active={false}
        running={false}
        blocked={false}
        finished={null}
        stopping={false}
        menuOpen={false}
        setMenuOpen={() => {}}
        onOpen={() => {}}
        onExport={() => {}}
        onSys={() => {}}
        onDelete={() => {}}
      />,
    )
    expect(screen.queryByLabelText('Run once now')).toBeNull()
    expect(screen.queryByLabelText('Resume schedule')).toBeNull()
    expect(screen.queryByLabelText('Stop run and pause schedule')).toBeNull()
  })
})
