import { describe, expect, it } from 'vitest'
import { parseModelScope } from './modelScope'

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
