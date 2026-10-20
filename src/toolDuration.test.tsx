// Issue #445: per-tool-call durations in the post-run trace.
//
// The store carries the server-measured durationMs (tool_result event and
// reloaded history), ToolChip renders it for finished calls, and a finished
// call WITHOUT one renders open-ended (no fake 0.0s). Client
// startedAt/finishedAt stays the live fallback for pre-#445 servers.
// Run: npx vitest run src/toolDuration.test.tsx
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { useAgent, buildMessages } from './store'
import { ToolCallRow } from './components'
import type { StoredMessage } from './api'

const row = (
  id: number,
  role: string,
  content: string,
  extra: Record<string, unknown> = {},
): StoredMessage =>
  ({
    id,
    role,
    content,
    images: null,
    sub_agent_transcript: null,
    tool_call_id: null,
    tool_calls: null,
    ...extra,
  }) as StoredMessage

describe('store carries server-measured durations (#445)', () => {
  beforeEach(() => {
    useAgent.setState({
      messagesByConv: {},
      statusByConv: {},
    } as never)
  })

  it('finishToolCall stamps durationMs from the tool_result event', () => {
    const s = useAgent.getState()
    useAgent.setState({
      messagesByConv: {
        t: [
          {
            id: 'a',
            role: 'assistant',
            content: '',
            toolCalls: [{ id: 'c1', name: 'bash', args: {}, startedAt: 1 }],
          },
        ],
      },
    } as never)
    s.finishToolCall('t', 'a', 'c1', { ok: true }, 1234)
    const tc = useAgent.getState().messagesByConv.t[0].toolCalls?.[0]
    expect(tc?.durationMs).toBe(1234)
    expect(tc?.finishedAt).toBeDefined()
  })

  it('omits durationMs when the event carries none (pre-#445 server)', () => {
    const s = useAgent.getState()
    useAgent.setState({
      messagesByConv: {
        t: [
          {
            id: 'a',
            role: 'assistant',
            content: '',
            toolCalls: [{ id: 'c1', name: 'bash', args: {}, startedAt: 1 }],
          },
        ],
      },
    } as never)
    s.finishToolCall('t', 'a', 'c1', { ok: true })
    const tc = useAgent.getState().messagesByConv.t[0].toolCalls?.[0]
    expect(tc?.durationMs).toBeUndefined()
  })

  it('subAgentToolResult stamps the nested call duration', () => {
    const s = useAgent.getState()
    useAgent.setState({
      messagesByConv: {
        t: [
          {
            id: 'a',
            role: 'assistant',
            content: '',
            toolCalls: [{ id: 'spawn-1', name: 'spawn_agent', args: {} }],
          },
        ],
      },
    } as never)
    s.startSubAgent('t', 'a', 'spawn-1', 1, 'explore', 'go')
    s.subAgentToolStart('t', 'a', 'spawn-1', 'inner-1', 'bash', {})
    s.subAgentToolResult('t', 'a', 'spawn-1', 'inner-1', { ok: true }, 77)
    const tools = useAgent.getState().messagesByConv.t[0].toolCalls?.[0].subAgent?.tools
    expect(tools?.[0]?.durationMs).toBe(77)
  })
})

describe('reloaded history keeps durations (#445)', () => {
  it('buildMessages threads duration_ms into pooled and nested calls', () => {
    const msgs = buildMessages([
      row(1, 'assistant', 'working', {
        tool_calls: [
          { id: 'c1', function: { name: 'bash', arguments: '{}' } },
          {
            id: 'sp1',
            function: {
              name: 'spawn_agent',
              arguments: JSON.stringify({ agent_type: 'explore', prompt: 'p' }),
            },
          },
        ],
      }),
      row(2, 'tool', JSON.stringify({ ok: 1 }), {
        tool_call_id: 'c1',
        tool_calls: [{ id: 'c1', function: { name: 'bash' } }],
        duration_ms: 4321,
      }),
      row(3, 'tool', JSON.stringify({ status: 'completed' }), {
        tool_call_id: 'sp1',
        tool_calls: [{ id: 'sp1', function: { name: 'spawn_agent' } }],
        duration_ms: 9000,
        sub_agent_transcript: {
          agent_type: 'explore',
          status: 'completed',
          transcript: [
            { role: 'user', content: 'p' },
            {
              role: 'tool',
              tool_call_id: 'inner-1',
              name: 'read_file',
              args: { path: 'a.txt' },
              content: JSON.stringify({ c: 1 }),
              duration_ms: 55,
            },
          ],
        },
      }),
    ])
    const calls = msgs[0].toolCalls ?? []
    expect(calls.find((c) => c.id === 'c1')?.durationMs).toBe(4321)
    const run = calls.find((c) => c.id === 'sp1')?.subAgent
    expect(run?.durationMs).toBe(9000)
    expect(run?.tools[0]?.durationMs).toBe(55)
  })

  it('a never-finished call has no durationMs (renders open-ended)', () => {
    // An assistant row that called a tool with no persisted result row:
    // the loop's synthetic "[no result recorded]" path - no duration.
    const msgs = buildMessages([
      row(1, 'assistant', '', {
        tool_calls: [{ id: 'ghost', function: { name: 'bash', arguments: '{}' } }],
      }),
      row(2, 'tool', JSON.stringify({ note: 'no result recorded' }), {
        tool_call_id: 'ghost',
        tool_calls: [{ id: 'ghost', function: { name: 'bash' } }],
        duration_ms: null,
      }),
    ])
    const tc = msgs[0].toolCalls?.[0]
    expect(tc?.result).toBeDefined()
    expect(tc?.durationMs).toBeUndefined()
  })
})

describe('ToolChip renders the duration (#445)', () => {
  it('finished chip shows the server duration', () => {
    render(
      <ToolCallRow
        tc={{
          id: 'c1',
          name: 'bash',
          args: { command: 'echo hi' },
          result: { ok: true },
          durationMs: 1234,
        }}
      />,
    )
    expect(screen.getAllByText('1.2s').length).toBeGreaterThan(0)
  })

  it('finished chip without a duration shows none (no fake 0.0s)', () => {
    render(
      <ToolCallRow
        tc={{ id: 'c1', name: 'bash', args: {}, result: { error: 'bad args' } }}
      />,
    )
    expect(screen.queryByText('0.0s')).toBeNull()
  })
})
