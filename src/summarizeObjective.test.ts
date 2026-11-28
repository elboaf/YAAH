import { describe, expect, it } from 'vitest'
import { summarizeObjective, OBJECTIVE_SUMMARY_LIMIT } from './summarizeObjective'

describe('summarizeObjective (#893) — display-only prompt summarization', () => {
  it('returns an empty string for empty / whitespace / non-string input', () => {
    expect(summarizeObjective('')).toBe('')
    expect(summarizeObjective('   \n\t  ')).toBe('')
    // @ts-expect-error — the guard exists to survive a bad store value
    expect(summarizeObjective(undefined)).toBe('')
  })

  it('passes short objectives through unchanged (only whitespace-collapsed)', () => {
    expect(summarizeObjective('ship the release')).toBe('ship the release')
    // Newlines in a pasted prompt collapse to single spaces.
    expect(summarizeObjective('fix the\nlogin bug')).toBe('fix the login bug')
  })

  it('collapses runs of internal whitespace to single spaces', () => {
    expect(summarizeObjective('add    tests\n\nfor  the auth module')).toBe(
      'add tests for the auth module',
    )
  })

  it('leaves text within the budget untouched (no ellipsis)', () => {
    const short = 'x'.repeat(OBJECTIVE_SUMMARY_LIMIT)
    expect(summarizeObjective(short)).toBe(short)
  })

  it('truncates long text at a word boundary and appends an ellipsis', () => {
    const long = Array.from({ length: 40 }, (_, i) => `word${i + 1}`).join(' ')
    const out = summarizeObjective(long)
    expect(out.endsWith('…')).toBe(true)
    // The body (minus the ellipsis) must be a prefix of the collapsed input —
    // nothing is invented, and a word is never split mid-letter.
    const body = out.slice(0, -1)
    expect(long.startsWith(body)).toBe(true)
    // The cut respects the budget (plus room for the ellipsis).
    expect(body.length).toBeLessThanOrEqual(OBJECTIVE_SUMMARY_LIMIT)
  })

  it('does not split a word when the boundary falls inside it', () => {
    // Force a situation where the last space before the limit is far back: a
    // run of one long token at the cut point.
    const filler = 'ab '.repeat(20) // 60 chars
    const longToken = 'z'.repeat(40) // no spaces inside
    const input = `${filler}${longToken} tail`
    const out = summarizeObjective(input)
    expect(out.endsWith('…')).toBe(true)
    // The truncated body must still be a prefix of the input (no mid-letter cut).
    const body = out.slice(0, -1)
    expect(input.startsWith(body)).toBe(true)
  })
})
