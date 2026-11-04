// PROTOTYPE (issue #643) — throwaway, do not ship.
//
// Question: how deep should the Terminal-as-Accent surface migration go?
//   A = "As shipped"     — today's look, the baseline (#629 state)
//   B = "Targeted accent" — machine output only: tool tape, rest chips,
//       trace/tape rows, code blocks go phosphor green; surfaces stay zinc
//   C = "Full migration"  — the whole picked system from the design-review
//       prototype (.scratch/yaah-retro-accent.html): #252525 panels, #333
//       hairlines, #0d1117 machine blocks with green text, bordered agent
//       message cards, blue-tinted user cards, restyled composer + sidebar
//
// This file is a PREVIEW LAYER, not an implementation: it overrides the real
// app's compiled Tailwind classes from the outside so the scope decision can
// be made on live data. The eventual real work (if C wins) edits the actual
// class names and DESIGN.md; nothing in this file is worth keeping.
//
// Switch: ?variant=a|b|c in the URL, the floating bar at the bottom, or the
// left/right arrow keys. Dev-only (import.meta.env.DEV) so a stray merge
// cannot ship it. Delete this file (and its App.tsx mount) once #643's
// verdict is folded in.

import { useEffect, useState } from 'react'

const VARIANTS = [
  { key: 'a', name: 'As shipped (today)' },
  { key: 'b', name: 'B · Targeted accent' },
  { key: 'c', name: 'C · Full migration' },
] as const

type VariantKey = (typeof VARIANTS)[number]['key']

function currentVariant(): VariantKey {
  const q = new URLSearchParams(window.location.search).get('variant')?.toLowerCase()
  return VARIANTS.some((v) => v.key === q) ? (q as VariantKey) : 'a'
}

const GREEN = '#58ff58'
const MONO = '"JetBrains Mono", Consolas, monospace'

// ---- variant stylesheets ----------------------------------------------------
// Each rule overrides the app's *compiled* Tailwind classes from the outside
// (this <style> tag is appended to <head>, so it wins same-specificity ties).
// Enumerated explicitly per class because Tailwind bakes alpha into each
// utility (bg-zinc-800/60 is its own class with its own opacity).

function variantCss(v: VariantKey): string {
  if (v === 'a') return ''

  // B — targeted accent. Surfaces stay zinc; only machine output gets the
  // terminal identity. This is exactly issue #643's item 2 (plus tape).
  if (v === 'b') {
    return `
/* telemetry tape: proof-of-output line → green mono */
[data-agent-telemetry] {
  color: ${GREEN};
  font-family: ${MONO};
}
/* rest-state tool chips: muted zinc → green mono (running chips already are) */
.bg-zinc-800\\/70.text-zinc-400, span.bg-zinc-800\\/70 {
  background-color: rgb(13 17 23 / 0.7);
  color: ${GREEN};
  font-family: ${MONO};
}
/* trace line + "did next" summary rows: collapsed call summaries → green */
button.font-mono.text-\\[11px\\].text-zinc-500,
p.font-mono.text-\\[11px\\].text-zinc-500 {
  color: ${GREEN};
  font-family: ${MONO};
}
/* code + diff views: the machine's voice → block on #0d1117, green text */
pre {
  color: ${GREEN};
  font-family: ${MONO};
  background-color: #0d1117;
}
`
  }

  // C — full migration: the picked system from the design prototype.
  return `
/* ---- identity layer (same as B) ---- */
[data-agent-telemetry] {
  color: ${GREEN};
  font-family: ${MONO};
}
span.bg-zinc-800\\/70 {
  background-color: rgb(13 17 23 / 0.7);
  color: ${GREEN};
  font-family: ${MONO};
}
/* collapsed trace/tape summary rows are machine voice too */
button.font-mono.text-\\[11px\\].text-zinc-500,
p.font-mono.text-\\[11px\\].text-zinc-500 {
  color: ${GREEN};
  font-family: ${MONO};
}
pre {
  color: ${GREEN};
  font-family: ${MONO};
  background-color: #0d1117;
}

/* ---- panel system: zinc surfaces → prototype palette ---- */
.bg-zinc-800              { background-color: #252525; }
.bg-zinc-800\\/70          { background-color: rgb(37 37 37 / 0.7); }
.bg-zinc-800\\/60          { background-color: rgb(37 37 37 / 0.6); }
.bg-zinc-800\\/50          { background-color: rgb(37 37 37 / 0.5); }
.bg-zinc-900              { background-color: #1b1b1b; }
.bg-zinc-900\\/70          { background-color: rgb(27 27 27 / 0.7); }
.bg-zinc-900\\/50          { background-color: rgb(27 27 27 / 0.5); }
.bg-zinc-950              { background-color: #0d1117; }
.bg-zinc-950\\/50          { background-color: rgb(13 17 23 / 0.5); }
.hover\\:bg-zinc-600:hover { background-color: #444444; }
.hover\\:bg-zinc-700:hover { background-color: #333333; }
.hover\\:bg-zinc-800:hover { background-color: #2e2e2e; }
.hover\\:bg-zinc-800\\/50:hover { background-color: rgb(55 55 55 / 0.5); }
.hover\\:bg-zinc-800\\/60:hover { background-color: rgb(55 55 55 / 0.6); }
.hover\\:bg-zinc-800\\/70:hover { background-color: rgb(55 55 55 / 0.7); }
.hover\\:bg-zinc-900:hover { background-color: #262626; }

.border-zinc-600           { border-color: #3a3a3a; }
.border-zinc-700           { border-color: #333333; }
.border-zinc-800           { border-color: #2e2e2e; }
.hover\\:border-zinc-500:hover { border-color: #444444; }

/* zinc-700 text (#3f3f46) is illegible on the #252525 panels */
.text-zinc-700 { color: #6b6b6b; }

/* ---- machine blocks: green on #0d1117 (the prototype's centerpiece) ---- */
[data-attachment-expand],
[data-agent-prompt-expand],
pre.bg-zinc-950 {
  color: ${GREEN};
  font-family: ${MONO};
  background-color: #0d1117;
}

/* ---- agent message rows → bordered cards (prototype .msg) ---- */
.border-l-2 {
  border: 1px solid #333333;
  border-left-width: 2px;
  border-left-color: #333333;
  background-color: #252525;
  border-radius: 8px;
  padding: 12px;
}
/* the "agent" label above the card body → warm muted, stays uppercase mono */
.border-l-2 > .mb-0\\.5 {
  color: #888888;
}
/* emission prose → the prototype's ink */
[data-agent-emission] {
  color: #d4d4d4;
}

/* ---- user messages → blue-tinted cards (prototype .msg.user) ---- */
.max-w-\\[85\\%\\].rounded {
  background-color: rgb(74 144 217 / 0.08);
  border: 1px solid rgb(74 144 217 / 0.45);
  color: #e6eef7;
}

/* ---- composer (prototype textarea) ---- */
#chat-input {
  background-color: #252525;
  border: 1px solid #444444;
  border-radius: 8px;
}
#chat-input:focus {
  border-color: #4a90d9;
  outline: none;
}

/* ---- sidebar: darker well + hairline (prototype .sidebar) ---- */
aside.w-64 {
  background-color: #1b1b1b;
  border-right: 1px solid #333333;
  margin-right: 8px;
}
`
}

// ---- the floating switcher (UI.md pattern) ----------------------------------

function PrototypeSwitcher({ current, onPick }: { current: VariantKey; onPick: (v: VariantKey) => void }) {
  const idx = VARIANTS.findIndex((v) => v.key === current)
  const cycle = (dir: 1 | -1) => {
    const next = VARIANTS[(idx + dir + VARIANTS.length) % VARIANTS.length]
    onPick(next.key)
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      if (e.key === 'ArrowLeft') cycle(-1)
      if (e.key === 'ArrowRight') cycle(1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })
  const variant = VARIANTS[idx]
  return (
    <div
      style={{
        position: 'fixed',
        bottom: 18,
        left: '50%',
        transform: 'translateX(-50%)',
        zIndex: 9999,
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        background: '#09090b',
        color: '#e4e4e7',
        border: '1px solid #3f3f46',
        borderRadius: 999,
        padding: '8px 14px',
        fontFamily: MONO,
        fontSize: 12,
        boxShadow: '0 8px 24px rgb(0 0 0 / 0.6)',
      }}
    >
      <button
        onClick={() => cycle(-1)}
        style={{ background: 'none', border: 'none', color: '#e4e4e7', cursor: 'pointer', fontSize: 14, padding: '0 4px' }}
        aria-label="previous variant"
      >
        ←
      </button>
      <span style={{ letterSpacing: '0.02em' }}>
        <strong style={{ color: GREEN }}>{variant.key.toUpperCase()}</strong> — {variant.name}
      </span>
      <button
        onClick={() => cycle(1)}
        style={{ background: 'none', border: 'none', color: '#e4e4e7', cursor: 'pointer', fontSize: 14, padding: '0 4px' }}
        aria-label="next variant"
      >
        →
      </button>
    </div>
  )
}

// ---- mount: inject the variant stylesheet, keep URL + bar in sync ------------

export function Prototype643SurfaceDepth() {
  const [variant, setVariant] = useState<VariantKey>(currentVariant)

  useEffect(() => {
    const url = new URL(window.location.href)
    if (variant === 'a') url.searchParams.delete('variant')
    else url.searchParams.set('variant', variant)
    window.history.replaceState(null, '', url)

    let tag = document.getElementById('proto-643-variant-css') as HTMLStyleElement | null
    if (!tag) {
      tag = document.createElement('style')
      tag.id = 'proto-643-variant-css'
      document.head.appendChild(tag)
    }
    tag.textContent = `/* PROTOTYPE #643 variant ${variant} — throwaway */\n${variantCss(variant)}`
  }, [variant])

  return <PrototypeSwitcher current={variant} onPick={setVariant} />
}
