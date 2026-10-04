// #206 — Speech normalization parity: the TS mirror (src/speech.ts) must
// produce IDENTICAL output to the Python normalizer (speak.py) on the
// shared cases, so live-stream client prep and backend synthesis agree.
// SHARED_CASES is kept in sync with backend/tests/test_speak.py.
import { describe, expect, it } from 'vitest'

import { normalizeForSpeech } from './speech'

const SHARED_CASES: Array<[string, string]> = [
  // Ports / technical digit runs go digit-by-digit.
  ['Shipped on port 443 today.', 'Shipped on port four four three today.'],
  ['Bind the server to localhost:8080.', 'Bind the server to localhost:eight oh eight oh.'],
  // Years read as years, not cardinals.
  ['It has worked since 1999.', 'It has worked since nineteen ninety-nine.'],
  ['Since 2016 the tool runs locally.', 'Since twenty sixteen the tool runs locally.'],
  // Version strings: version word, digit groups, suffix letters spelled.
  ['Now running v1.0.16-rc.7.', 'Now running version one point oh point sixteen R C point seven.'],
  // Dotted quads (IPs) group with "dot".
  ['Server lives at 10.0.0.1.', 'Server lives at ten dot oh dot oh dot one.'],
  // Two-group decimals are left for the engine (it reads them correctly).
  ['Pi is about 3.14 already.', 'Pi is about 3.14 already.'],
  // Known acronyms are spelled out.
  ['TLS 1.3 is enabled.', 'T L S 1.3 is enabled.'],
  ['The API gateway failed.', 'The A P I gateway failed.'],
  // Phone numbers group with a pause.
  ['Call 555-0100 for access.', 'Call five five five, oh one oh oh for access.'],
  // Emotion markers are stripped, not read.
  ['Great news! [excited] It works.', 'Great news! It works.'],
  ['[whispers] Quietly done.', 'Quietly done.'],
  // SSML fragments are stripped, not mangled into phonemes.
  ['Say it <break time="500ms"/> slowly.', 'Say it slowly.'],
  // IPA in brackets survives (the documented escape hatch for names).
  ['The name is [dʒeɪson].', 'The name is [dʒeɪson].'],
  // Currency (#292): the $ never reaches synthesis (no "dollar" prefix),
  // the decimal point is not "point", thousands commas don't mangle.
  ['may the best $19.99 win', 'may the best nineteen ninety-nine win'],
  ['$1,299.50 at checkout', 'one thousand two hundred ninety-nine fifty at checkout'],
  ['That totals $5.', 'That totals five dollars.'],
  ['under $0.75 total', 'under seventy-five cents total'],
  ['$12 subtotal', 'twelve dollars subtotal'],
]

describe('normalizeForSpeech (#206 mirror of speak.py)', () => {
  it.each(SHARED_CASES)('normalizes %s', (source, expected) => {
    expect(normalizeForSpeech(source)).toBe(expected)
  })

  it('is idempotent', () => {
    const once = normalizeForSpeech('Backported to v2.10.3 in 2024, see port 8443.')
    expect(normalizeForSpeech(once)).toBe(once)
  })

  it('strips unlisted emotion markers but keeps bracketed IPA', () => {
    expect(normalizeForSpeech('Done [sighs] at last.')).toBe('Done at last.')
    expect(normalizeForSpeech('The name is [dʒeɪson].')).toBe('The name is [dʒeɪson].')
  })

  it('leaves non-currency numbers alone', () => {
    // The currency rule owns $-glued amounts ONLY: bare decimals, version
    // triples, years, phones and IPs keep today's behavior.
    expect(normalizeForSpeech('Pi is about 3.14.')).toBe('Pi is about 3.14.')
    expect(normalizeForSpeech('Running 1.0.5 now.')).toBe('Running one point oh point five now.')
    expect(normalizeForSpeech('Shipped in 1999.')).toBe('Shipped in nineteen ninety-nine.')
    expect(normalizeForSpeech('Call 555-0100.')).toBe('Call five five five, oh one oh oh.')
    expect(normalizeForSpeech('Host is 10.0.0.1.')).toBe('Host is ten dot oh dot oh dot one.')
  })
})
