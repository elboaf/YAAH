import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

  it('keeps parent and sub-agent telemetry segments on one line', () => {
    useAgent.setState({
      tapeByConv: { '42': '\nspawned explore\nspawn explore: read_file\nspawn explore: bash' },
    })
    const run = {
      agentId: 1,
      agentType: 'explore',
      prompt: 'inspect this module',
      status: 'running' as const,
      text: '',
      tools: [],
      telemetry: '\nspawned explore\nread_file\nsearch_files',
      preview: 'latest answer fragment',
    }
    render(
      <MessageView
        msg={liveMessage([{ id: 'spawn-1', name: 'spawn_agent', subAgent: run }])}
        live
      />,
    )

    const telemetry = document.querySelectorAll('[data-agent-telemetry]')
    expect(telemetry.length).toBe(2)
    for (const strip of telemetry) {
      expect(strip.textContent).not.toMatch(/[\r\n]/)
    }
  })

  it('keeps collapsed sub-agent chat to a one-line latest-emission preview while showing live tools and telemetry', () => {
    const run = {
      agentId: 1,
      agentType: 'explore',
      prompt: 'inspect this module',
      status: 'running' as const,
      text: 'earlier emission\nlatest answer fragment',
      preview: 'latest answer fragment',
      tools: [{ id: 'nested-1', name: 'read_file', args: { path: 'src/store.ts' }, startedAt: 1 }],
      telemetry: 'nested thinking telemetry',
    }
    render(
      <MessageView
        msg={liveMessage([{ id: 'spawn-1', name: 'spawn_agent', subAgent: run, contentOffset: 0 }])}
        live
      />,
    )

    expect(document.querySelector('[data-subagent-preview-line]')?.textContent).toContain('latest answer fragment')
    expect(screen.queryByText('earlier emission')).toBeNull()
    expect(screen.getByText('read_file')).toBeTruthy()
    expect(screen.getByText('nested thinking telemetry')).toBeTruthy()
    const preview = document.querySelector('[data-subagent-preview]')
    expect(preview?.classList.contains('overflow-hidden')).toBe(true)
    expect(document.querySelector('[data-subagent-tool-ticker]')).toBeTruthy()

    fireEvent.click(document.querySelector('[data-subagent-toggle]')!)
    expect(screen.getByText(/earlier emission/)).toBeTruthy()
    expect(screen.getByText(/latest answer fragment/)).toBeTruthy()
  })

  it('places the sub-agent card between parent-agent emissions and preserves all sub-agent summaries when settled', () => {
    const run = {
      agentId: 1,
      agentType: 'explore',
      prompt: 'inspect this module',
      status: 'completed' as const,
      text: 'sub-agent full answer',
      preview: 'sub-agent full answer',
      tools: [{ id: 'nested-1', name: 'read_file', args: { path: 'src/store.ts' }, result: { ok: true } }],
      telemetry: 'sub-agent telemetry',
    }
    const message: ChatMessage = {
      ...liveMessage([{ id: 'spawn-1', name: 'spawn_agent', subAgent: run, contentOffset: 'parent before'.length + 1 }]),
      content: 'parent before\nparent after',
    }
    render(<MessageView msg={message} />)

    const emissions = [...document.querySelectorAll('[data-agent-emission]')]
    const card = document.querySelector('[data-subagent-card]')!
    expect(emissions).toHaveLength(2)
    expect(emissions[0].textContent).toContain('parent before')
    expect(emissions[0].compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(card.compareDocumentPosition(emissions[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText('sub-agent telemetry')).toBeTruthy()
    expect(screen.getByRole('button', { name: /1 call/ })).toBeTruthy()
    expect(document.querySelector('[data-subagent-full-text]')).toBeNull()

    fireEvent.click(document.querySelector('[data-subagent-toggle]')!)
    expect(document.querySelector('[data-subagent-full-text]')?.textContent).toContain('sub-agent full answer')
  })

  it('renders nested sub-agent tool calls in a single clipped ticker row', () => {
    const run = {
      agentId: 1,
      agentType: 'explore',
      prompt: 'inspect this module',
      status: 'running' as const,
      text: 'Looking through the code',
      tools: [
        {
          id: 'nested-1',
          name: 'read_file',
          args: { path: 'src/store.ts' },
          startedAt: 1,
        },
        {
          id: 'nested-2',
          name: 'bash',
          args: { command: 'npm test' },
          result: { output: 'ok' },
          startedAt: 1,
          finishedAt: 2,
        },
      ],
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
    fireEvent.click(document.querySelector('[data-subagent-toggle]')!)
    expect(screen.getByText('Looking through the code')).toBeTruthy()
    const ticker = document.querySelector('[data-subagent-tool-ticker]')
    expect(ticker).toBeTruthy()
    expect(ticker!.classList.contains('overflow-hidden')).toBe(true)
    expect(ticker!.classList.contains('flex')).toBe(true)
    expect(ticker!.querySelectorAll('[data-tape-align]').length).toBe(1)
    expect(screen.getByText('read_file')).toBeTruthy()
    expect(screen.getByText('bash')).toBeTruthy()
  })

  it('collapses completed sub-agent tool calls to the normal expandable trace', () => {
    const run = {
      agentId: 1,
      agentType: 'explore',
      prompt: 'inspect this module',
      status: 'completed' as const,
      text: 'Finished reading',
      tools: [
        {
          id: 'nested-1',
          name: 'read_file',
          args: { path: 'src/store.ts' },
          result: { output: 'done' },
        },
      ],
      telemetry: 'completed per-agent tape',
    }
    render(
      <MessageView
        msg={liveMessage([{ id: 'spawn-1', name: 'spawn_agent', subAgent: run }])}
        live
      />,
    )
    expect(screen.getByRole('button', { name: /1 call/ })).toBeTruthy()
    expect(document.querySelector('[data-subagent-tool-ticker]')).toBeNull()
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
