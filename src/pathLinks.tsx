// Chat path linkification (#258). Agent messages constantly reference
// workspace files; this module turns path-shaped plain text in assistant
// messages into links that open the file in the existing PreviewModal.
//
// Detection is deliberately two-stage (issue #258 scope, decided in triage):
//   1. syntactic: must contain a path separator and end in a plausible file
//      segment, must not be (part of) a URL or an absolute path;
//   2. existence: confirmed against the workspace via a containment-checked
//      backend probe — a purely syntactic pass would flood prose with false
//      links. Non-existent candidates render as plain text.
//
// Never linkified: URLs, fenced code blocks, inline code spans and markdown
// link targets (those never reach the text renderer; fences/spans are also
// skipped here), or absolute paths (`C:\...`, `/usr/...` — opening those
// needs a new backend command + security review).
//
// `path:42`, `path:42:7` and `path#L42` suffixes are parsed and tolerated:
// the suffix is stripped for the opened path while the raw text (suffix
// included) stays the link label. Scrolling to the line is deferred — the
// PreviewModal has no line targeting yet, so the link must simply not break.

import { Fragment, useEffect, useState, type ReactNode } from 'react'
import { checkFileExists } from './api'

/**
 * One linkifiable token: `raw` is the exact labelled substring (suffix
 * included), `path` is the suffix-stripped workspace-relative path to open,
 * `line` the tolerated-but-ignored line suffix (null when absent).
 */
export interface PathToken {
  raw: string
  path: string
  line: number | null
}

/** A syntactic candidate located in its line: [start, end) covers the full
 *  label (any `:42` / `#L42` suffix INCLUDED — the label keeps it), while
 *  `path` is the suffix-stripped path to probe and open. */
interface Candidate {
  path: string
  line: number | null
  start: number
  end: number
}

/**
 * What a token may start with / contain. Stops at whitespace and at plain
 * sentence punctuation so "in src/app.ts." or "(see src/app.ts)" tokenize
 * cleanly; extension-less names (Makefile) still work. `:` and `#` are
 * included so a run covers its `:42` / `#L42` suffix (peeled later); URLs
 * and host:port shapes that pick up those characters are rejected by
 * isUrlLike below.
 */
const LEAD = /[/\\A-Za-z0-9_.@+:#-]/

/** Candidates must carry a separator AND a plausible file segment. */
const HAS_SEPARATOR = /[/\\]/

/** A final path segment: path characters, no separators. */
const FILE_SEG = /^[A-Za-z0-9_.@+-]+$/

/** Optional `:line[:col]` or `#L42` suffix after the path proper. */
const SUFFIX = /(?::(\d{1,6})(?::(\d{1,6}))?|#L(\d{1,6}))$/

/** Sentence punctuation that may directly follow a token in prose. */
const TRAILING_PUNCT = /[.,;:!?)\]}'"\u2019\u201d]$/

const lastSeg = (tok: string) => tok.split(/[/\\]/).pop() ?? ''

/** Anything with a scheme-looking head, a www host, a bare domain tail, or
 *  a host:port shape is prose about the web, not a workspace path. */
function isUrlLike(tok: string): boolean {
  return (
    /^[A-Za-z][A-Za-z0-9+.-]*:/.test(tok) ||
    /^www\./i.test(tok) ||
    /^[\w.-]+\.(com|org|net|io|dev)\b/i.test(tok) ||
    /^[\w.-]+:\d{1,5}(?:[\\/]|$)/.test(tok)
  )
}

/** Absolute paths (`C:\...`, `\\server\share`, `/usr/...`) are out of scope. */
function isAbsolute(tok: string): boolean {
  return /^[A-Za-z]:[\\/]/.test(tok) || /^[/\\]/.test(tok)
}

/**
 * Stage 1 (one line of plain text): scan for syntactic path candidates.
 * Consumes each maximal token run whole, then peels trailing sentence
 * punctuation and an optional line/column suffix off the tail, so interior
 * segments ("src", "app" of "src/app.ts") are never re-matched as tokens
 * and a confirmed path can never splice into a longer one ("src/app.ts"
 * inside "src/app.tsx"). Deduplicates identical spans.
 */
export function tokenizePathCandidates(line: string): Candidate[] {
  const out: Candidate[] = []
  const seen = new Set<string>()
  let i = 0
  while (i < line.length) {
    if (!LEAD.test(line[i])) {
      i++
      continue
    }
    const start = i
    let j = i
    while (j < line.length && LEAD.test(line[j])) j++
    let tok = line.slice(i, j)
    let end = j

    // Peel sentence punctuation off the tail ("src/app.ts." -> "src/app.ts",
    // "src/app.ts:" after a suffix peel -> "src/app.ts").
    while (tok.length > 0 && (TRAILING_PUNCT.test(lastSeg(tok)) || tok.endsWith(':'))) {
      tok = tok.slice(0, -1)
      end--
    }

    // Strip a line/column suffix before the shape checks, so a bare-number
    // tail never fails the file-segment test. `end` keeps pointing past the
    // suffix: the label stays whole ("src/app.ts:42"), only `path` strips.
    let lineNo: number | null = null
    const suf = SUFFIX.exec(tok)
    if (suf) {
      lineNo = Number(suf[1] ?? suf[3])
      tok = tok.slice(0, suf.index)
    }

    if (
      tok.length >= 3 &&
      HAS_SEPARATOR.test(tok) &&
      FILE_SEG.test(lastSeg(tok)) &&
      !isUrlLike(tok) &&
      !isAbsolute(tok)
    ) {
      const key = `${tok}\u0000${start}`
      if (!seen.has(key)) {
        seen.add(key)
        out.push({ path: tok, line: lineNo, start, end })
      }
    }
    i = end > start ? end : start + 1
  }
  return out
}

/** Split a raw string into its code-significant lines: fenced blocks are
 *  skipped entirely, inline code spans are masked (same length, so the
 *  word boundaries a candidate starts/ends on survive the scan). */
function* scannableLines(raw: string): Generator<string> {
  let fence: string | null = null // opening marker run, e.g. '```'
  for (const line of raw.split('\n')) {
    const trimmed = line.trim()
    if (fence !== null) {
      // Closing fence: the same marker run and nothing else on the line.
      if (trimmed.startsWith(fence) && /^[`~]+\s*$/.test(trimmed)) fence = null
      continue
    }
    const open = /^(`{3,}|~{3,})/.exec(trimmed)
    if (open) {
      fence = open[1]
      continue
    }
    // Inline code spans AND markdown link constructs: neither is plain chat
    // text. Code interiors are invisible to the scanner; inside a link's
    // parentheses the path is a destination (handled by the markdown parser),
    // and a label renders through the `a` component, not the text renderer.
    // Masked same-length so the word boundaries a candidate needs survive.
    yield line
      .replace(/`+[^`]*`+/g, (m) => ' '.repeat(m.length))
      .replace(/\[[^\]]*\]\([^)]*\)/g, (m) => ' '.repeat(m.length))
  }
}

/**
 * Stage 1 over whole raw markdown: the deduplicated set of candidate paths
 * in the message's plain text (code fences/spans excluded).
 */
export function collectPathCandidates(raw: string): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  for (const line of scannableLines(raw)) {
    for (const t of tokenizePathCandidates(line)) {
      if (!seen.has(t.path)) {
        seen.add(t.path)
        out.push(t.path)
      }
    }
  }
  return out
}

/**
 * Stage 2: keep only candidates that exist in the workspace. One batched
 * round of probes per message, resolved concurrently; any failure (backend
 * down, non-JSON error page) simply means "not a link". Positive results
 * are cached per workspace with a short TTL so streaming repaints don't
 * refetch every token, while files created moments ago still linkify.
 */
const existsCache = new Map<string, { ok: boolean; at: number }>()
const EXISTS_TTL_MS = 15_000

export async function resolveExistingPaths(
  workspace: string | null,
  candidates: string[],
): Promise<Set<string>> {
  const found = new Set<string>()
  if (!workspace || workspace === '.') return found
  const uniq = [...new Set(candidates)]
  const now = Date.now()
  await Promise.all(
    uniq
      .filter((p) => {
        const hit = existsCache.get(`${workspace}\u0000${p}`)
        return !hit || now - hit.at > EXISTS_TTL_MS
      })
      .map(async (p) => {
        const ok = await checkFileExists(workspace, p).catch(() => false)
        existsCache.set(`${workspace}\u0000${p}`, { ok, at: Date.now() })
      }),
  )
  for (const p of uniq) {
    if (existsCache.get(`${workspace}\u0000${p}`)?.ok) found.add(p)
  }
  return found
}

/** Test hook: drop the existence cache. */
export function clearPathCache(): void {
  existsCache.clear()
}

const EMPTY_SET: ReadonlySet<string> = new Set()

/**
 * Stage 2 as state for one message: resolves the message's candidates once
 * per (raw, workspace) change and returns the confirmed-existing subset.
 * Starts empty — a message paints exactly as before, then upgrades to
 * linkified once the (fast, batched) probes come back.
 */
export function usePathTokens(raw: string, workspace: string | null): ReadonlySet<string> {
  const [confirmed, setConfirmed] = useState<ReadonlySet<string>>(EMPTY_SET)
  useEffect(() => {
    let live = true
    const candidates = collectPathCandidates(raw)
    if (candidates.length === 0) {
      setConfirmed(EMPTY_SET)
      return
    }
    resolveExistingPaths(workspace, candidates).then((s) => {
      if (live) setConfirmed(s)
    })
    return () => {
      live = false
    }
  }, [raw, workspace])
  return confirmed
}

/** Plain text with remark-breaks semantics: a literal \n renders as a break. */
function withBreaks(s: string, keyBase: string): ReactNode {
  const segs = s.split('\n')
  return (
    <Fragment key={keyBase}>
      {segs.map((seg, i) => (
        <Fragment key={`${keyBase}.${i}`}>
          {i > 0 && <br />}
          {seg}
        </Fragment>
      ))}
    </Fragment>
  )
}

function fileAnchor(
  t: PathToken,
  key: string,
  onOpen: (path: string, line: number | null) => void,
): ReactNode {
  return (
    <a
      key={key}
      href="#"
      data-chat-file-link={t.path}
      onClick={(e) => {
        e.preventDefault()
        onOpen(t.path, t.line)
      }}
      title={`Open ${t.path} in the file preview`}
      className="text-sky-400 underline decoration-sky-400/40 underline-offset-2 hover:text-sky-300"
    >
      {t.raw}
    </a>
  )
}

/**
 * The synchronous text renderer: plain text (with line breaks) where the
 * candidates confirmed to exist become anchors. Used by AgentMarkdown's
 * `p`/`li`/`td` renderers — code spans and fenced blocks never reach it
 * (react-markdown routes those to their own components). With an empty
 * `confirmed` set the output is the message's ordinary rendering.
 */
export function renderTextWithPaths(
  text: string,
  confirmed: ReadonlySet<string>,
  onOpen: (path: string, line: number | null) => void,
): ReactNode {
  const toks = tokenizePathCandidates(text).filter((t) => confirmed.has(t.path))
  if (toks.length === 0) return withBreaks(text, 't')
  const parts: ReactNode[] = []
  let pos = 0
  for (let k = 0; k < toks.length; k++) {
    const t = toks[k]
    if (t.start > pos) parts.push(withBreaks(text.slice(pos, t.start), `p${k}`))
    parts.push(fileAnchor({ raw: text.slice(t.start, t.end), path: t.path, line: t.line }, `a${k}`, onOpen))
    pos = t.end
  }
  if (pos < text.length) parts.push(withBreaks(text.slice(pos), `p${toks.length}`))
  return parts
}
