// Tests for #275 slice 2: emission segments whose React keys survive
// mid-stream boundary insertions. The old renderer keyed segments by the
// running cursor offset, so a new anchor arriving mid-stream shifted every
// later key; React replaced those DOM nodes and the browser's selection (a
// DOM range) was destroyed. The fixed segmentation keys each segment by the
// anchor that ENDS it (immutable call id) and the growing tail by a fixed
// `-end` key — identity that insertion cannot move.

import { describe, expect, it } from 'vitest'
import { emissionSegments } from './emissionSegments'
import type { ChatMessage, ToolCall } from './store'

let n = 0
const call = (name: string, extra: Partial<ToolCall> = {}): ToolCall => ({
  id: `call-${++n}`,
  name,
  ...extra,
})

const msg = (id: string, over: Partial<ChatMessage>): ChatMessage => ({
  id,
  role: 'assistant',
  content: '',
  ...over,
})

const SUB: ToolCall['subAgent'] = {
  agentId: 1,
  agentType: 'explore',
  prompt: 'p',
  status: 'running',
  text: '',
  tools: [],
  telemetry: '',
}

describe('emissionSegments (#275)', () => {
  it('plain streaming message (no anchors): one stable tail segment', () => {
    expect(emissionSegments(msg('m1', { content: 'hello' }))!.map((s) => s.key)).toEqual([
      'seg-m1-end',
    ])
    expect(
      emissionSegments(msg('m1', { content: 'hello world, streaming on' }))!.map((s) => s.key),
    ).toEqual(['seg-m1-end'])
  })

  it('a boundary arriving mid-stream does NOT shift existing keys', () => {
    // Turn shape: text streamed, ask_user asked (offset at the boundary),
    // answer recorded, text resumed. Before the anchor exists the whole
    // text is the tail; after, the head becomes its own keyed segment and
    // the tail keeps its fixed key.
    const ask = call('ask_user', { result: {}, contentOffset: 10 })
    expect(emissionSegments(msg('m2', { content: 'first part' }))!.map((s) => s.key)).toEqual([
      'seg-m2-end',
    ])

    const segs = emissionSegments(
      msg('m2', { content: 'first partsecond part', toolCalls: [ask] }),
    )!
    expect(segs.map((s) => s.key)).toEqual([
      `seg-m2-${ask.id}`,
      `anchor-${ask.id}`,
      'seg-m2-end',
    ])
    expect(segs[0].content).toBe('first part')
    expect(segs[2].content).toBe('second part')
  })

  it('a second anchor insertion shifts NOTHING before it', () => {
    const ask1 = call('ask_user', { result: {}, contentOffset: 5 })
    const keysMid = emissionSegments(msg('m3', { content: 'aaaaabbbbb', toolCalls: [ask1] }))!.map(
      (s) => s.key,
    )
    const spawn = call('spawn_agent', { contentOffset: 15, subAgent: SUB })
    const segs = emissionSegments(
      msg('m3', { content: 'aaaaabbbbbbcccccccccc', toolCalls: [ask1, spawn] }),
    )!
    // ask1's segment keys are unchanged; only new keys appear after it.
    expect(segs.map((s) => s.key).slice(0, 2)).toEqual(keysMid.slice(0, 2))
    expect(segs.map((s) => s.key)).toContain(`anchor-${spawn.id}`)
    // ordering follows offsets
    const ks = segs.map((s) => s.key)
    expect(ks.indexOf(`anchor-${ask1.id}`)).toBeLessThan(ks.indexOf(`anchor-${spawn.id}`))
  })

  it('out-of-order toolCalls array still yields offset-ordered segments', () => {
    const ask = call('ask_user', { result: {}, contentOffset: 12 })
    const spawn = call('spawn_agent', { contentOffset: 4, subAgent: SUB })
    const segs = emissionSegments(
      msg('m4', { content: 'aaaabbbbbbbbbb', toolCalls: [ask, spawn] }),
    )!
    expect(segs[0].key).toBe(`seg-m4-${spawn.id}`)
    expect(segs[0].content).toBe('aaaa')
    expect(segs[1].key).toBe(`anchor-${spawn.id}`)
    expect(segs[2].key).toBe(`seg-m4-${ask.id}`)
    expect(segs[2].content).toBe('bbbbbbbb')
  })

  it('legacy rows with an answered-but-offset-less ask return null (caller fallback)', () => {
    const plainAsk = call('ask_user') // not answered: not an anchor at all
    const legacyAsk = call('ask_user', { result: {} }) // answered, NO offset
    expect(emissionSegments(msg('m5', { content: 'x', toolCalls: [legacyAsk] }))).toBeNull()
    // an unanswered ask without a result is simply not an anchor
    expect(emissionSegments(msg('m5', { content: 'x', toolCalls: [plainAsk] }))).toEqual([
      { key: 'seg-m5-end', content: 'x' },
    ])
  })
})
