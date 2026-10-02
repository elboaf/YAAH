// Regression (voice symptom 2, "doubling"): heuristicBriefing's closing was
// sliced as tail.slice(0, lastSentenceEnd) — i.e. from the START of the final
// paragraph — so a multi-sentence tail spoke the WHOLE paragraph back after
// the opening line, repeating the message nearly verbatim. Mirrors
// backend/tests/test_speak.py::test_heuristic_briefing_closing_is_last_sentence_not_whole_paragraph.
import { describe, expect, it } from 'vitest'

import { heuristicBriefing, SAY_MAX_CHARS, spokenLine } from './speech'

describe('heuristicBriefing closing sentence', () => {
  const md = [
    'The migration ran clean and the API tests are green.',
    'Mid-report detail about retry budgets that nobody needs read aloud. '.repeat(6),
    '',
    'Deploy is set for Friday morning.',
  ].join('\n')

  it('closing is the LAST sentence, not the whole final paragraph', () => {
    const out = heuristicBriefing(md)
    expect(out.startsWith('The migration ran clean and the API tests are green.')).toBe(true)
    expect(out.endsWith('Deploy is set for Friday morning.')).toBe(true)
    expect(out).not.toContain('retry budgets')
    expect(out.length).toBeLessThanOrEqual(SAY_MAX_CHARS)
  })

  it('a multi-sentence tail keeps only its final sentence', () => {
    const out = heuristicBriefing('Intro sentence one. Intro sentence two.\n\nWrap one. Wrap two. Final word here.')
    expect(out).toBe('Intro sentence one. Final word here.')
  })

  it('spokenLine fallback never doubles the body', () => {
    const out = spokenLine(null, md)
    expect(out).not.toContain('retry budgets')
    expect(out.endsWith('Deploy is set for Friday morning.')).toBe(true)
  })
})
