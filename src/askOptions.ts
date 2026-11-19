// #564: ask_user `options` normalization. The model owns the payload, so any
// ingestion point that trusts its shape (array of {label}) can be handed a
// string, object, or worse — and every renderer does `options.map(...)`.
// One bad shape unmounts the whole React tree (no error boundary existed):
// the "uniform grey window" of #564. Normalize at ingestion, render never
// sees anything but a clean array.

export interface AskOption {
  label: string
  description?: string
}

/** Coerce whatever arrived into `AskOption[]` — never throws, never returns
 *  anything but an array. Non-array input, array elements that aren't
 *  objects with a string `label`, and missing labels are all dropped.
 *
 *  A string payload is a real model failure mode, not a hypothetical: the
 *  model sometimes double-encodes the array, passing the JSON text of the
 *  options where the array itself belongs. Three saved calls in the live
 *  database did exactly that. A JSON string that parses back to an array
 *  is recovered; anything else collapses to [] so a renderer can never
 *  reach `.map` on a non-array. */
export function normalizeAskOptions(value: unknown): AskOption[] {
  const raw = typeof value === 'string' ? decodeStringOptions(value) : value
  if (!Array.isArray(raw)) return []
  const out: AskOption[] = []
  for (const o of raw) {
    if (typeof o !== 'object' || o === null) continue
    const label = (o as { label?: unknown }).label
    if (typeof label !== 'string' || !label) continue
    const description = (o as { description?: unknown }).description
    out.push(
      typeof description === 'string' && description
        ? { label, description }
        : { label },
    )
  }
  return out
}

/** One decode attempt for a double-encoded payload. The model's output can
 *  also carry a trailing fragment after the array (a real saved row ended
 *  its array and then appended the question object), so a strict parse is
 *  tried first and a bracket-balanced prefix second. */
function decodeStringOptions(value: string): unknown {
  const text = value.trim()
  if (!text.startsWith('[')) return undefined
  try {
    return JSON.parse(text)
  } catch {
    /* fall through to the balanced-prefix attempt */
  }
  const end = balancedArrayEnd(text)
  if (end === -1) return undefined
  try {
    return JSON.parse(text.slice(0, end + 1))
  } catch {
    return undefined
  }
}

/** Index of the `]` closing the array that opens at text[0], or -1. Tracks
 *  string state so brackets inside labels/descriptions are ignored. */
function balancedArrayEnd(text: string): number {
  let depth = 0
  let inString = false
  let escaped = false
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (inString) {
      if (escaped) escaped = false
      else if (ch === '\\') escaped = true
      else if (ch === '"') inString = false
      continue
    }
    if (ch === '"') inString = true
    else if (ch === '[') depth++
    else if (ch === ']') {
      depth--
      if (depth === 0) return i
    }
  }
  return -1
}
