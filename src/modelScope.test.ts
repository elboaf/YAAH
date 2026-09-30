import { describe, expect, it } from 'vitest'
import { parseModelScope, qualifyModelScope } from './modelScope'

describe('parseModelScope', () => {
  it('parses a provider-qualified model into the model selector value', () => {
    expect(parseModelScope('openai::gpt-4.1')).toEqual({ provider: 'openai', model: 'gpt-4.1' })
  })

  it('keeps unqualified model ids visible', () => {
    expect(parseModelScope('gpt-4.1')).toEqual({ provider: '', model: 'gpt-4.1' })
  })

  it('preserves additional separators in the model id', () => {
    expect(parseModelScope('custom::model::variant')).toEqual({ provider: 'custom', model: 'model::variant' })
  })
})

describe('qualifyModelScope', () => {
  it('qualifies a bare id with the active provider', () => {
    expect(qualifyModelScope('gpt-4.1', 'openai')).toBe('openai::gpt-4.1')
  })

  it('leaves already-qualified values untouched', () => {
    expect(qualifyModelScope('groq::llama-3', 'openai')).toBe('groq::llama-3')
  })

  it('keeps empty scopes empty (deliberate Default)', () => {
    expect(qualifyModelScope('', 'openai')).toBe('')
  })

  it('returns the bare id when no provider is known', () => {
    expect(qualifyModelScope('gpt-4.1', '')).toBe('gpt-4.1')
  })

  it('is idempotent', () => {
    const once = qualifyModelScope('gpt-4.1', 'openai')
    expect(qualifyModelScope(once, 'openai')).toBe(once)
  })

  it('trims stray whitespace', () => {
    expect(qualifyModelScope('  gpt-4.1  ', 'openai')).toBe('openai::gpt-4.1')
  })
})
