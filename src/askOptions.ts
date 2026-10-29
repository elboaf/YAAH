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
 *  objects with a string `label`, and missing labels are all dropped. */
export function normalizeAskOptions(value: unknown): AskOption[] {
  if (!Array.isArray(value)) return []
  const out: AskOption[] = []
  for (const o of value) {
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
