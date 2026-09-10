// Lightweight code rendering helpers: token highlighting, line numbers,
// copy buttons, and a naive diff view. No external deps.

export interface Token {
  text: string
  cls: string
}

const KEYWORDS =
  /\b(abstract|as|async|await|break|case|catch|class|const|continue|def|default|delete|do|elif|else|except|export|extends|False|finally|for|from|function|global|if|import|in|instanceof|interface|is|lambda|let|match|new|None|not|or|and|pass|private|protected|public|raise|readonly|return|self|static|struct|super|switch|this|throw|True|try|type|typeof|var|void|while|with|yield|fn|impl|pub|mut|use|where)\b/

// Tokenize one line into (text, css-class) spans.
export function highlightLine(line: string): Token[] {
  const tokens: Token[] = []
  let rest = line
  let guard = 0
  while (rest.length > 0 && guard++ < 500) {
    // comments
    const commentIdx = searchComment(rest)
    if (commentIdx !== null && commentIdx.pos === 0) {
      tokens.push({ text: rest, cls: 'text-zinc-500 italic' })
      return tokens
    }
    // strings
    const str = /^("(?:[^"\\]|\\.)*?"|'(?:[^'\\]|\\.)*?'|`(?:[^`\\]|\\.)*?`)/.exec(rest)
    if (str) {
      tokens.push({ text: str[0], cls: 'text-emerald-300' })
      rest = rest.slice(str[0].length)
      continue
    }
    // numbers
    const num = /^(0x[0-9a-fA-F]+|\d+(\.\d+)?)/.exec(rest)
    if (num) {
      tokens.push({ text: num[0], cls: 'text-orange-300' })
      rest = rest.slice(num[0].length)
      continue
    }
    // keywords
    const kw = KEYWORDS.exec(rest)
    if (kw && kw.index === 0) {
      tokens.push({ text: kw[0], cls: 'text-violet-300 font-semibold' })
      rest = rest.slice(kw[0].length)
      continue
    }
    const kwLater = KEYWORDS.exec(rest)
    const nextStop = firstOf(commentIdx?.pos ?? -1, kwLater?.index ?? -1)
    const take = nextStop > 0 ? nextStop : rest.length
    tokens.push({ text: rest.slice(0, take), cls: '' })
    rest = rest.slice(take)
  }
  return tokens
}

function firstOf(...vals: Array<number | null>): number {
  let min = -1
  for (const v of vals) {
    if (v !== null && v !== undefined && v >= 0 && (min === -1 || v < min)) min = v
  }
  return min
}

function searchComment(line: string): { pos: number } | null {
  for (const marker of ['#', '//', '/*', '--']) {
    const idx = line.indexOf(marker)
    // crude: skip string-quoted occurrences by counting quotes before idx
    if (idx >= 0) {
      const before = line.slice(0, idx)
      const quotes = (before.match(/"/g) ?? []).length + (before.match(/'/g) ?? []).length
      if (quotes % 2 === 0) return { pos: idx }
    }
  }
  return null
}

export function langOf(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? ''
  const map: Record<string, string> = {
    ts: 'typescript', tsx: 'typescript', js: 'javascript', jsx: 'javascript',
    py: 'python', rs: 'rust', go: 'go', json: 'json', md: 'markdown',
    html: 'html', css: 'css', sh: 'bash', yml: 'yaml', yaml: 'yaml',
    toml: 'toml', sql: 'sql', c: 'c', cpp: 'cpp', h: 'c',
  }
  return map[ext] ?? 'text'
}

// ------------------------------------------------------------- naive diff

export interface DiffLine {
  kind: 'ctx' | 'del' | 'add'
  text: string
}

/** Line diff of old vs new (LCS-based; fine for typical edit sizes). */
export function diffLines(oldText: string, newText: string): DiffLine[] {
  const a = oldText.split('\n')
  const b = newText.split('\n')
  const n = a.length
  const m = b.length
  // LCS table (cap size to avoid blowups on huge edits)
  if (n * m > 1_000_000) {
    return [
      ...a.map((t): DiffLine => ({ kind: 'del', text: t })),
      ...b.map((t): DiffLine => ({ kind: 'add', text: t })),
    ]
  }
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ kind: 'ctx', text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ kind: 'del', text: a[i++] })
    } else {
      out.push({ kind: 'add', text: b[j++] })
    }
  }
  while (i < n) out.push({ kind: 'del', text: a[i++] })
  while (j < m) out.push({ kind: 'add', text: b[j++] })
  return out
}
