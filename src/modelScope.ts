/** Split a provider-qualified model value at its first separator. */
export function parseModelScope(value: string): { provider: string; model: string } {
  const separator = value.indexOf('::')
  if (separator < 0) return { provider: '', model: value }
  return { provider: value.slice(0, separator), model: value.slice(separator + 2) }
}

/**
 * #132: a stored chat scope must be COMPLETE (`provider::model`) or EMPTY
 * (follow the global default) — never a bare id. A bare id has no routing
 * provider, so the turn drifts to whichever provider the sidebar default
 * points at later ("model not found" when the new provider doesn't host the
 * id). Bare values qualify with the active provider; qualified and empty
 * values pass through untouched. Idempotent.
 */
export function qualifyModelScope(value: string, activeProvider: string): string {
  const trimmed = (value || '').trim()
  if (!trimmed) return ''
  if (trimmed.includes('::')) return trimmed
  return activeProvider ? `${activeProvider}::${trimmed}` : trimmed
}
