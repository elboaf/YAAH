// Issue #255: provider-injected `<system_*>` control text must never become
// a chat title, and a sidebar row whose title already carries it must show
// a dedicated amber warning triangle (issue #25 status slot) with an
// actionable /handoff tooltip instead of the raw provider text.

import { describe, expect, it } from 'vitest'

import { stripProviderMarkup } from './providerMarkup'
import { hasProviderMarkup } from './components'

describe('stripProviderMarkup (#255)', () => {
  it('strips a leading closed system_warning block', () => {
    const text = '<system_warning>⚠️ CONTEXT LOW - Prioritize completing current tasks</system_warning> fix the login bug'
    expect(stripProviderMarkup(text)).toBe('fix the login bug')
  })

  it('strips consecutive provider blocks', () => {
    const text = '<system_warning>a</system_warning> <system_notice>b</system_notice> real prompt'
    expect(stripProviderMarkup(text)).toBe('real prompt')
  })

  it('consumes the rest of an unclosed provider tag', () => {
    expect(stripProviderMarkup('<system_warning>⚠️ CONTEXT L')).toBe('')
  })

  it('leaves ordinary text untouched', () => {
    expect(stripProviderMarkup('help me fix the flaky test suite')).toBe(
      'help me fix the flaky test suite',
    )
    // Non-system tags and mid-text markup are content, not control text.
    expect(stripProviderMarkup('what does <system_prompt> mean?')).toBe(
      'what does <system_prompt> mean?',
    )
  })
})

describe('hasProviderMarkup (#255)', () => {
  it('detects a leading system tag in a stored title', () => {
    expect(hasProviderMarkup('<system_warning>⚠️ CONTEXT L…')).toBe(true)
    expect(hasProviderMarkup('Real chat title')).toBe(false)
  })
})
