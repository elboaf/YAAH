/** Split a provider-qualified model value at its first separator. */
export function parseModelScope(value: string): { provider: string; model: string } {
  const separator = value.indexOf('::')
  if (separator < 0) return { provider: '', model: value }
  return { provider: value.slice(0, separator), model: value.slice(separator + 2) }
}
