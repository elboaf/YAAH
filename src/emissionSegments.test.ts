// Tests for #275 slice 2: emission segments whose React keys survive
// mid-stream boundary insertions. The old renderer keyed segments by the
// running cursor offset, so a new anchor arriving mid-stream shifted every
// later key; React replaced those DOM nodes and the browser's selection (a
// DOM range) was destroyed. The fixed segmentation keys each TEXT segment
// by the cursor offset where its text STARTS: an anchor's arrival only ever
// splits the tail, and the surviving tail keeps its key because its start
// offset is unchanged — insertion cannot move any existing key.

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
      'seg-m1-0',
    ])
    expect(
      emissionSegments(msg('m1', { content: 'hello world, streaming on' }))!.map((s) => s.key),
    ).toEqual(['seg-m1-0'])
  })

  it('a boundary arriving mid-stream does NOT shift existing keys', () => {
    // Turn shape: text streamed, ask_user asked (offset at the boundary),
    // answer recorded, text resumed. Before the anchor exists the whole
    // text is the tail; after, the head becomes its own keyed segment and
    // the tail keeps its fixed key.
    const ask = call('ask_user', { result: {}, contentOffset: 10 })
    expect(emissionSegments(msg('m2', { content: 'first part' }))!.map((s) => s.key)).toEqual([
      'seg-m2-0',
    ])

    const segs = emissionSegments(
      msg('m2', { content: 'first partsecond part', toolCalls: [ask] }),
    )!
    expect(segs.map((s) => s.key)).toEqual([
      `seg-m2-0`,
      `anchor-${ask.id}`,
      'seg-m2-10',
    ])
    expect(segs[0].content).toBe('first part')
    // the tail keeps its key: it starts at the same cursor (10) it started at
    // before the anchor arrived
    expect(segs[2].key).toBe('seg-m2-10')
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
    expect(segs[0].key).toBe('seg-m4-0')
    expect(segs[0].content).toBe('aaaa')
    expect(segs[1].key).toBe(`anchor-${spawn.id}`)
    expect(segs[2].key).toBe('seg-m4-4')
    expect(segs[2].content).toBe('bbbbbbbb')
  })

  it('an anchor arriving at the END of the current text does NOT change the tail key', () => {
    // Regression (CodeRabbit return trip): the tail used to be keyed
    // `seg-<id>-end`; when an anchor landed at the current end-of-text the
    // tail's key became `seg-<id>-<anchor.id>` and React REPLACED the text
    // node, clearing an in-progress selection. Keying the tail (and every
    // text segment) by its STARTING cursor offset fixes this: the anchor's
    // text segment has zero width (cut === cursor, emits nothing) and the
    // surviving tail keeps key `seg-<id>-<cursor>`.
    const ask = call('ask_user', { result: {}, contentOffset: 5 })
    expect(emissionSegments(msg('m6', { content: 'hello' }))!.map((s) => s.key)).toEqual([
      'seg-m6-0',
    ])
    const segs = emissionSegments(msg('m6', { content: 'hello', toolCalls: [ask] }))!
    // same key as before the anchor arrived, now on the head segment; the
    // anchor emits at the end and no tail follows
    expect(segs.map((s) => s.key)).toEqual(['seg-m6-0', 'anchor-' + ask.id])
    expect(segs.map((s) => s.content)).toEqual(['hello', ''])
  })

  it('legacy rows with an answered-but-offset-less ask return null (caller fallback)', () => {
    const plainAsk = call('ask_user') // not answered: not an anchor at all
    const legacyAsk = call('ask_user', { result: {} }) // answered, NO offset
    expect(emissionSegments(msg('m5', { content: 'x', toolCalls: [legacyAsk] }))).toBeNull()
    // an unanswered ask without a result is simply not an anchor
    expect(emissionSegments(msg('m5', { content: 'x', toolCalls: [plainAsk] }))).toEqual([
      { key: 'seg-m5-0', content: 'x' },
    ])
  })
})
