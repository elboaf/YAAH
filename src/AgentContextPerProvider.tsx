/** Per-provider "Agent & context" editor, shown inside the expanded
 *  provider row on the Providers tab (issue #111). It renders ONE ENTRY PER
 *  CONFIGURED MODEL; each entry owns its model (catalog dropdown) and max
 *  steps. There is NO provider-level max steps. The context window is NOT
 *  a setting: the detected value (provider report -> built-in table) shows
 *  read-only as the reference for the context gauge. */

import type React from 'react'
import { useState } from 'react'

/** Family base of a model id: router-slot prefixes ("1.", "x2.",
 *  digits/short tokens before a known local-model family) collapse so an
 *  entry saved under one alias groups with its siblings. Settings uses it
 *  only to GROUP saved entries under the right provider's editor. */
const FAMILY_STEMS = [
  'tiel', 'qwen', 'llama', 'mistral', 'phi', 'gemma', 'deepseek', 'glm',
  'kimi', 'codestral', 'mixtral', 'smollm', 'falcon', 'starcoder',
  'wizardcoder',
]

export function familyBaseId(model: string): string {
  const m = /^(?:(?:[a-z]|[a-z]?\d){1,4})\./.exec(model)
  if (m) {
    const rest = model.slice(m[0].length)
    if (FAMILY_STEMS.some((stem) => rest.toLowerCase().startsWith(stem))) return rest
  }
  return model
}

/** Saved per-model entry ids that belong to THIS provider's editor: every
 *  saved id whose family base matches the provider's default model or any
 *  catalog model, plus the default model itself (shown even without an
 *  entry, as the auto-fallback case). Deduplicated, saved ids first. */
export function providerEntryModels(
  savedIds: string[],
  defaultModel: string | undefined,
  catalog: string[],
): string[] {
  const familyOf = (id: string) => familyBaseId(id).toLowerCase()
  const families = new Set([
    ...(defaultModel ? [familyOf(defaultModel)] : []),
    ...catalog.map(familyOf),
  ])
  return [
    ...new Set([
      ...savedIds.filter((id) => families.has(familyOf(id))),
      ...(defaultModel ? [defaultModel] : []),
    ]),
  ]
}

/** One configured model's settings + change handlers (owned by the parent). */
export interface ModelEntry {
  model: string
  /** Per-model step budget; '' = untouched (default applies). */
  steps: number | ''
  onSteps: (v: number | '') => void
  /** Detected (auto-resolved) window for this model, or null. */
  ctxAuto: number | null
}

interface AgentCtxPerProviderProps {
  /** Provider name being edited. */
  name: string
  /** Model catalog for this provider (may be empty when unreachable). */
  models: string[]
  /** One entry per configured model, in display order. */
  entries: ModelEntry[]
  /** Register a model id (dropdown pick or free-typed) as a new entry. */
  onAddModel: (id: string) => void
  /** Remove a model entry from this provider's editor. */
  onRemoveModel?: (id: string) => void
  maxStepsDefault: number
  inputCls: string
}

export function AgentContextPerProvider({
  name,
  models,
  entries,
  onAddModel,
  onRemoveModel,
  maxStepsDefault,
  inputCls,
}: AgentCtxPerProviderProps) {
  /** Inline "add model" affordance: dropdown first (catalog), free-type as
   *  the explicit fallback for ids the catalog doesn't list. */
  const [adding, setAdding] = useState(false)
  const [freeType, setFreeType] = useState(false)
  const [newModel, setNewModel] = useState('')
  const submitNewModel = () => {
    const id = newModel.trim()
    if (!id) return
    onAddModel(id)
    setNewModel('')
    setFreeType(false)
    setAdding(false)
  }

  const close = () => {
    setAdding(false)
    setFreeType(false)
    setNewModel('')
  }

  /** Catalog models not already configured as entries. */
  const addable = models.filter((m) => !entries.some((e) => e.model === m))

  return (
    <div className="mt-2.5 border-t border-zinc-800 pt-2.5" data-agent-ctx={name}>
      <h4 className="mb-2 font-mono text-[10px] font-medium uppercase tracking-[0.1em] text-zinc-500">
        Agent &amp; context
      </h4>

      {entries.map((e) => (
        <div
          key={e.model}
          data-model-entry={e.model}
          className="mb-2.5 rounded border border-zinc-800 p-2"
        >
          {/* The entry header IS the model identity: everything below sits
              visually inside this model's scope. */}
          <div className="mb-1.5 flex items-center gap-2">
            <span className="font-mono text-[11px] text-zinc-200">{e.model}</span>
            {onRemoveModel && entries.length > 1 && (
              <button
                type="button"
                className="ml-auto text-[10px] text-zinc-600 hover:text-red-400"
                onClick={() => onRemoveModel(e.model)}
                aria-label={`remove model entry ${e.model}`}
              >
                remove
              </button>
            )}
          </div>

          {/* Per-model max steps (issue #111 comment: per-model ONLY, no
              provider-level field anywhere). */}
          <label className="mb-1 block text-[10px] text-zinc-500">max steps</label>
          <input
            type="number"
            min="0"
            className={`${inputCls} w-full`}
            value={e.steps === '' ? maxStepsDefault : e.steps}
            onChange={(ev) => e.onSteps(ev.target.value === '' ? '' : Number(ev.target.value))}
            aria-label={`max steps for ${e.model}`}
          />
          <p className="mt-0.5 text-[10px] text-zinc-600">
            {maxStepsDefault} by default · 0 = unlimited (Stop still works)
          </p>

          <p className="mt-1 font-mono text-[10px] text-zinc-600">
            {e.ctxAuto != null
              ? `context window (detected): ${e.ctxAuto.toLocaleString()} tokens`
              : 'context window: not detected — no % readout for this model'}
          </p>
        </div>
      ))}

      {/* Add another entry: the catalog dropdown first, free-type as an
          explicit fallback for ids the catalog doesn't list. Unlimited
          entries are allowed. */}
      {adding ? (
        freeType ? (
          <span className="mt-1 flex gap-1.5">
            <input
              className={`${inputCls} min-w-0 flex-1`}
              placeholder="model id (e.g. openai/gpt-5.2)"
              value={newModel}
              autoFocus
              onChange={(e) => setNewModel(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  submitNewModel()
                } else if (e.key === 'Escape') {
                  close()
                }
              }}
              aria-label={`model id to add for ${name}`}
            />
            <button
              type="button"
              className="shrink-0 rounded border border-zinc-700 px-2 text-[11px] text-zinc-300 hover:bg-zinc-800"
              onClick={submitNewModel}
            >
              add
            </button>
            <button
              type="button"
              className="shrink-0 rounded px-1 text-[11px] text-zinc-500 hover:text-zinc-300"
              onClick={close}
              aria-label="Cancel adding a model"
            >
              cancel
            </button>
          </span>
        ) : (
          <span className="mt-1 flex items-center gap-1.5">
            <select
              className={`${inputCls} min-w-0 flex-1`}
              value=""
              onChange={(e) => {
                if (e.target.value) {
                  onAddModel(e.target.value)
                  close()
                }
              }}
              aria-label={`model to add for ${name}`}
            >
              <option value="">{addable.length ? 'choose a model…' : 'no models in catalog'}</option>
              {addable.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="shrink-0 rounded px-1 text-[10px] text-zinc-500 hover:text-zinc-300"
              onClick={() => setFreeType(true)}
              aria-label="type a model id instead"
            >
              type id…
            </button>
            <button
              type="button"
              className="shrink-0 rounded px-1 text-[11px] text-zinc-500 hover:text-zinc-300"
              onClick={close}
              aria-label="Cancel adding a model"
            >
              cancel
            </button>
          </span>
        )
      ) : (
        <button
          type="button"
          className="mt-1 block text-[10px] text-zinc-500 hover:text-zinc-300"
          onClick={() => setAdding(true)}
        >
          + add a model to configure
        </button>
      )}
    </div>
  )
}
