// Build-output canary: fails the build if critical Tailwind utilities are
// missing from the emitted CSS.
//
// Why this exists (rc.39 incident, 2026-10-22): a dependabot PR half-migrated
// Tailwind v3 -> v4 (deps + postcss config swapped, but src/index.css kept the
// v3 `@tailwind` directives, which the v4 engine ignores). The app still
// built -- vite emitted a *valid but nearly empty* stylesheet -- so the only
// signal was end users seeing an unstyled UI. This canary turns that failure
// mode into a hard build failure instead.
//
// It asserts on utilities from every layer that silently vanished that day:
//   - .bg-zinc-900  : theme-dependent color utility (theme layer compiled)
//   - .flex {       : plain structural utility (utility layer present at all)
//   - JetBrains Mono: theme.fontFamily override from tailwind.config.js
// If a redesign genuinely removes one of these, update the list here
// deliberately -- the failure message says so.
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const assetsDir = join(process.cwd(), 'dist', 'assets')
let css
try {
  const cssFiles = readdirSync(assetsDir).filter((f) => f.endsWith('.css'))
  if (cssFiles.length === 0) {
    console.error('CSS canary: no .css file found in dist/assets -- did vite build run?')
    process.exit(1)
  }
  css = cssFiles.map((f) => readFileSync(join(assetsDir, f), 'utf8')).join('\n')
} catch (err) {
  console.error(`CSS canary: cannot read dist/assets -- ${err.message}`)
  process.exit(1)
}

// Each canary: [needle, what its absence means]
const canaries = [
  ['.bg-zinc-900', 'theme-dependent color utility missing -- the Tailwind theme layer did not compile'],
  ['.flex{', 'basic structural utilities missing -- the Tailwind utility layer did not compile'],
  ['JetBrains Mono', 'font-family theme override missing -- fonts will fall back to browser defaults'],
]

const missing = canaries.filter(([needle]) => !css.includes(needle))
if (missing.length > 0) {
  console.error('\nCSS canary FAILED -- the built stylesheet is missing:')
  for (const [, reason] of missing) console.error(`  - ${reason}`)
  console.error(
    '\nIf Tailwind was just upgraded: check src/index.css for the correct entry' +
      '\ndirectives for that major version (v3 `@tailwind` directives are ignored' +
      '\nby the v4 engine, which needs `@import "tailwindcss";`).' +
      '\nIf the design deliberately dropped one of these utilities, update the' +
      "\ncanary list in scripts/check-css.js.",
  )
  process.exit(1)
}
console.log('CSS canary: ok -- theme, utilities, and fonts present in the built stylesheet.')
