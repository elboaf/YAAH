/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        mono: ['"JetBrains Mono"', 'Consolas', 'monospace'],
      },
      // Issue #643 (Terminal-as-Accent, full migration — variant C of the
      // prototype on prototype/643-surface-depth): the zinc ladder is
      // retuned to the chosen surface system. Every bg-zinc-*/text-zinc-*/
      // border-zinc-* utility in src picks the new value up without touching
      // a single className. Mapping, by what each step MEANS in src:
      //   zinc-950 #09090b → #0d1117  machine blocks / deep wells (prototype .tool)
      //   zinc-900 #18181b → #1b1b1b  sidebar / header wells (darker than panels)
      //   zinc-800 #27272a → #252525  panels, chips, inputs (prototype .msg bg)
      //   zinc-700 #3f3f46 → #333333  hairlines + strong borders (prototype border)
      //   zinc-600 #52525b → #555555  faint text (timestamps, glyphs) — kept at
      //     today's dimness, warmed; NOT a border tone (borders live on 700)
      //   zinc-500 #71717a → #8a8a8a  muted ink (prototype #888)
      //   zinc-400 #a1a1aa → #a8a8a8  secondary ink, warmed to neutral gray
      //   zinc-300 #d4d4d8 → #d4d4d4  body ink (prototype body color exactly)
      //   zinc-200 #e4e4e7 → #e6e6e6  bright ink, warmed
      // 50/100 unchanged. #58ff58 stays reserved for machine voice
      // (.terminal-text / .crt-glow in index.css) — it is deliberately NOT
      // in this ladder.
      colors: {
        zinc: {
          50: '#fafafa',
          100: '#f4f4f5',
          200: '#e6e6e6',
          300: '#d4d4d4',
          400: '#a8a8a8',
          500: '#8a8a8a',
          600: '#555555',
          700: '#333333',
          800: '#252525',
          900: '#1b1b1b',
          950: '#0d1117',
        },
      },
    },
  },
  plugins: [],
}
