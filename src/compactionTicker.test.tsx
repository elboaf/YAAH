import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MessageView } from './components'
import { useAgent, type ChatMessage } from './store'

const compacted = { summarized: 4, summary: 'Earlier context' }
const tape = 'new telemetry event'

function liveMessage(toolCalls: ChatMessage['toolCalls'] = []): ChatMessage {
  return {
    id: 'assistant-live',
    role: 'assistant',
    content: '',
    toolCalls,
  }
}

beforeEach(() => {
  useAgent.setState({
    conversationId: 42,
    compactionByConv: { '42': compacted },
    tapeByConv: { '42': tape },
  })
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get: () => 500,
  })
  Object.defineProperty(HTMLElement.prototype, 'offsetLeft', {
    configurable: true,
    get: () => 80,
  })
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
    configurable: true,
    get: () => 40,
  })
})

afterEach(() => {
  cleanup()
  useAgent.setState({ compactionByConv: {}, tapeByConv: {} })
  Reflect.deleteProperty(HTMLElement.prototype, 'clientWidth')
  Reflect.deleteProperty(HTMLElement.prototype, 'offsetLeft')
  Reflect.deleteProperty(HTMLElement.prototype, 'offsetWidth')
})

describe('live compaction ticker', () => {
  it('keeps the telemetry strip visible on a compaction-only turn', () => {
    render(<MessageView msg={liveMessage()} live />)

    expect(screen.getByRole('button', { name: /context compacted/ })).toBeTruthy()
    expect(screen.getByText(tape)).toBeTruthy()
  })

  it('renders a live sub-agent block with its own telemetry strip', () => {
    const run = {
      agentId: 1,
      agentType: 'explore',
      prompt: 'inspect this module',
      status: 'running' as const,
      text: 'Looking through the code',
      tools: [],
      telemetry: 'nested per-agent tape',
    }
    render(
      <MessageView
        msg={liveMessage([{ id: 'spawn-1', name: 'spawn_agent', subAgent: run }])}
        live
      />,
    )
    expect(screen.getByText('inspect this module')).toBeTruthy()
    expect(screen.getByText('nested per-agent tape')).toBeTruthy()
    expect(screen.getByText('Looking through the code')).toBeTruthy()
  })

  it('places later traces ahead of compaction and keeps them aligned with telemetry', async () => {
    render(
      <MessageView
        msg={liveMessage([{ id: 'tool-1', name: 'bash', args: { command: 'echo hi' } }])}
        live
      />,
    )

    const alignment = document.querySelector('[data-tape-align]')
    const compactionChip = screen.getByRole('button', { name: /context compacted/ })
    expect(alignment).toBeTruthy()
    expect(alignment!.compareDocumentPosition(compactionChip) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    await waitFor(() => expect(screen.getByText(tape)).toBeTruthy())
  })
})
