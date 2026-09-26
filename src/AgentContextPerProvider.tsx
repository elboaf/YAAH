/** Per-provider "Agent & context" editor, shown inside the expanded
 *  provider row on the Providers tab. Max steps are per provider; context
 *  window and history compaction are per provider+model (model chosen
 *  from the provider's catalog, not typed). The context-window field is
 *  pre-filled with the model's detected default; the compaction trigger
 *  pre-fills with the shipped 300k default. */

import type React from 'react'
import { useState } from 'react'

interface AgentCtxPerProviderProps {
  /** Provider name being edited. */
  name: string
  /** Model catalog for this provider (may be empty when unreachable). */
  models: string[]
  /** Selected model id (draft, per provider). */
  modelSel: string
  onModelSel: (m: string) => void
  /** This provider's model field (for the "current model" hint). */
  currentModel: string
  maxSteps: number | ''
  onMaxSteps: (v: number | '') => void
  /** Context window draft (tokens) for the selected model. */
  ctxDraft: number | ''
  onCtxDraft: (v: number | '') => void
  /** Detected (unoverridden) window for the selected model, or null. */
  ctxAuto: number | null
  ctxSaved: number | undefined
  /** Compaction drafts. */
  compEnabled: boolean | undefined
  onCompEnabled: (v: boolean) => void
  compK: number | ''
  onCompK: (v: number | '') => void
  /** Default trigger shown in the field (k tokens). */
  compactionDefaultK: number
  /** Default max steps shown in the field. */
  maxStepsDefault: number
  /** Add a model id not present in the catalog (e.g. an unreleased or
   *  custom-routed model) and make it the configured one. */
  onAddModel: (id: string) => void
  inputCls: string
}

export function AgentContextPerProvider({
  name,
  models,
  modelSel,
  onModelSel,
  currentModel,
  maxSteps,
  onMaxSteps,
  ctxDraft,
  onCtxDraft,
  ctxAuto,
  ctxSaved,
  compEnabled,
  onCompEnabled,
  compK,
  onCompK,
  compactionDefaultK,
  maxStepsDefault,
  onAddModel,
  inputCls,
}: AgentCtxPerProviderProps) {
  /** The model being configured: explicit selection, else the provider's
   *  current model (hydrated into the selection by the parent). */
  const model = modelSel || currentModel

  /** Model switch: the parent re-hydrates the drafts for the new model
   *  (its hydration effect keys on the selection per provider). */
  const onModelChange = onModelSel

  /** Inline "add model" affordance: type a model id not in the catalog and
   *  configure it. Small state, local to this editor. */
  const [adding, setAdding] = useState(false)
  const [newModel, setNewModel] = useState('')
  const submitNewModel = () => {
    const id = newModel.trim()
    if (!id) return
    onAddModel(id)
    setNewModel('')
    setAdding(false)
  }

  return (
    <div className="mt-2.5 border-t border-zinc-800 pt-2.5" data-agent-ctx={name}>
      <h4 className="mb-2 font-mono text-[10px] font-medium uppercase tracking-[0.1em] text-zinc-500">
        Agent &amp; context
      </h4>

      {/* Max steps: per provider, 200 by default */}
      <div className="max-w-sm">
        <label className="mb-1 block text-[10px] text-zinc-500">Max steps</label>
        <input
          type="number"
          min="0"
          className={`${inputCls} w-full`}
          value={maxSteps === '' ? maxStepsDefault : maxSteps}
          onChange={(e) => onMaxSteps(e.target.value === '' ? '' : Number(e.target.value))}
          aria-label={`${name} max steps`}
        />
        <p className="mt-1 text-[10px] text-zinc-600">
          {maxStepsDefault} by default · 0 = unlimited (Stop still works)
        </p>
      </div>

      {/* Context window override: per provider+model, dropdown selection */}
      <div className="mt-2.5 border-t border-zinc-800 pt-2.5">
        <label className="mb-1 block text-[10px] text-zinc-500">Model</label>
        <select
          className={`${inputCls} w-full`}
          value={model}
          onChange={(e) => onModelChange(e.target.value)}
          aria-label={`Model to configure for ${name}`}
        >
          {models.length === 0 && <option value="">{model || 'no models listed'}</option>}
          {models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
          {model && !models.includes(model) && <option value={model}>{model}</option>}
        </select>

        {/* Add a model the catalog doesn't list (unreleased ids, custom
            OpenRouter routes) and start configuring it immediately. */}
        {adding ? (
          <span className="mt-1.5 flex gap-1.5">
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
                  setAdding(false)
                  setNewModel('')
                }
              }}
              aria-label="New model id to configure"
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
              onClick={() => {
                setAdding(false)
                setNewModel('')
              }}
              aria-label="Cancel adding a model"
            >
              cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="mt-1.5 block text-[10px] text-zinc-500 hover:text-zinc-300"
            onClick={() => setAdding(true)}
          >
            + add another model to configure
          </button>
        )}

        <label className="mb-1 mt-2.5 block text-[10px] text-zinc-500">Context window override</label>
        <p className="mb-1.5 text-[10px] text-zinc-600">
          Tokens for this model — wins over the detected value (powers the context readout in the chat panel).
        </p>
        <div className="flex gap-1.5">
          <input
            type="number"
            min="0"
            placeholder={ctxAuto ? String(ctxAuto) : 'auto'}
            aria-label={`Context window in tokens for ${model}`}
            className={`${inputCls} w-40 shrink-0`}
            value={ctxDraft}
            onChange={(e) => onCtxDraft(e.target.value === '' ? '' : Number(e.target.value))}
          />
          {ctxAuto != null && (
            <span className="self-center font-mono text-[10px] text-zinc-600">
              detected: {ctxAuto.toLocaleString()}
            </span>
          )}
          {ctxSaved != null && (
            <span className="self-center font-mono text-[10px] text-blue-400">
              saved: {ctxSaved.toLocaleString()}
            </span>
          )}
        </div>
      </div>

      {/* History compaction: per provider+model, 300k default */}
      <div className="mt-2.5 border-t border-zinc-800 pt-2.5">
        <label className="mb-1 block text-[10px] text-zinc-500">History compaction</label>
        <div className="flex items-center gap-2.5">
          <button
            type="button"
            role="switch"
            aria-checked={compEnabled ?? true}
            aria-label={`Enable history compaction for ${model}`}
            className={`relative h-4 w-8 shrink-0 rounded-full transition-colors ${
              (compEnabled ?? true) ? 'bg-blue-600' : 'bg-zinc-700'
            }`}
            onClick={() => onCompEnabled(!(compEnabled ?? true))}
          >
            <span
              className={`absolute top-0.5 h-3 w-3 rounded-full bg-zinc-100 transition-all ${
                (compEnabled ?? true) ? 'left-4.5' : 'left-0.5'
              }`}
            />
          </button>
          <span className="text-[10px] text-zinc-400">
            {(compEnabled ?? true) ? 'On' : 'Off'} — fold old history into a summary when the prompt grows past
          </span>
          <input
            type="number"
            min="0"
            aria-label="Compaction trigger threshold in thousands of tokens"
            className={`${inputCls} w-20 shrink-0`}
            value={compK}
            onChange={(e) => onCompK(e.target.value === '' ? '' : Number(e.target.value))}
          />
          <span className="text-[10px] text-zinc-600">k tokens (default {compactionDefaultK}k)</span>
        </div>
        <p className="mt-1 text-[10px] text-zinc-600">
          The trigger is the smaller of the threshold and 70% of the window, so small-window models still compact
          before overflowing. Blank restores the {compactionDefaultK}k default.
        </p>
      </div>
    </div>
  )
}
