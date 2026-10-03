// Agent chat markdown rendering. react-markdown + remark-gfm, mapped onto the
// app's existing zinc design language. Code blocks reuse the same CodeBlock
// used by the file preview / file panel. No dangerouslySetInnerHTML anywhere:
// react-markdown builds elements, so untrusted content cannot inject markup.
// Links are additionally restricted to http/https/mailto schemes.
//
// #275 (selection stability): EVERY element component ReactMarkdown uses is a
// module-level constant. React reconciles by element-type identity, so a
// component defined inside a render body (or built fresh per render, as the
// old `components(confirmed, onOpenPath)` factory did) is a NEW type on every
// streamed delta — React unmounted and remounted the entire markdown DOM per
// delta, destroying any in-progress text selection even with perfectly stable
// segment keys (#281). Message-specific values (confirmed path tokens, the
// preview opener) flow through MarkdownContext instead of closures.

import {
  Fragment,
  createContext,
  useContext,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'
import { highlightLine } from './codeview'
import { openExternal } from './openExternal'
import { stripSay } from './speech'
import { useAgent } from './store'
import {
  usePathTokens,
  fileAnchor,
  tokenizePathCandidates,
} from './pathLinks'

// ---------------------------------------------------------------- code views

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      className="rounded px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true)
          setTimeout(() => setCopied(false), 1200)
        })
      }}
    >
      {copied ? 'copied!' : 'copy'}
    </button>
  )
}

/** Syntax-highlighted code with line numbers (Q43). */
export function CodeBlock({ code, lang, startLine = 1 }: { code: string; lang?: string; startLine?: number }) {
  const lines = code.replace(/\n$/, '').split('\n')
  return (
    <div className="my-1 overflow-hidden rounded border border-zinc-700 bg-zinc-950">
      <div className="flex items-center justify-between border-b border-zinc-800 bg-zinc-900 px-2 py-1">
        <span className="font-mono text-[10px] text-zinc-500">{lang ?? 'text'}</span>
        <CopyButton text={code} />
      </div>
      <pre className="max-h-96 overflow-auto p-1 font-mono text-[11px] leading-4">
        {lines.map((line, i) => (
          <div key={i} className="flex">
            <span className="w-10 shrink-0 select-none pr-2 text-right text-zinc-600">
              {startLine + i}
            </span>
            <span className="whitespace-pre-wrap break-all text-zinc-300">
              {highlightLine(line).map((t, j) => (
                <span key={j} className={t.cls}>{t.text}</span>
              ))}
            </span>
          </div>
        ))}
      </pre>
    </div>
  )
}

// ---------------------------------------------------------------- links

const SAFE_URL = /^(https?:|mailto:)/i

function safeHref(url?: string): string | undefined {
  if (!url) return undefined
  try {
    // Resolve relative URLs against a dummy base, then only allow real schemes.
    const abs = new URL(url, 'https://chat.invalid')
    return SAFE_URL.test(abs.protocol) ? url : undefined
  } catch {
    return undefined
  }
}

// ---------------------------------------------------------------- helpers

/** Recursively pull plain text out of a React node tree (for code blocks). */
function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'object' && 'props' in node) {
    const el = node as { props?: { children?: ReactNode } }
    return textOf(el.props?.children)
  }
  return ''
}

// ---------------------------------------------------------------- agent markdown

/** Type of the click handler that opens a linked path in the preview. */
type PathOpener = (path: string, line: number | null) => void

/**
 * Message-scoped values the element components need. Context, not closures:
 * a closure over per-message state would make every component a fresh type
 * per message render (#275 — see the file header).
 */
interface MarkdownScope {
  /** Path tokens confirmed to exist in the workspace (#258). */
  confirmed: ReadonlySet<string>
  /** Opens a confirmed path in the file preview. */
  onOpenPath: PathOpener
}

const MarkdownContext = createContext<MarkdownScope>({
  confirmed: new Set<string>(),
  onOpenPath: () => {},
})

/** Plain-text node(s) with #258 linkification applied. Renders ordinary
 *  markdown text nodes through renderTextWithPaths; non-text children
 *  (emphasis, nested links, chips...) are passed through untouched — the
 *  scanner only ever sees top-level plain runs, so formatting survives and
 *  a markdown link's label/target can never be rewritten. */
function pathAware(children: ReactNode): ReactNode {
  const { confirmed, onOpenPath } = useContext(MarkdownContext)
  return toArray(children).map((child, i) => {
    if (typeof child !== 'string' || child.length === 0) return child
    // #275: streamed plain text renders append-only (see StreamText) so a
    // delta never rewrites the text node an in-progress selection anchors
    // in. Confirmed linkification splits the line into static runs and
    // path links; each static run is its own StreamText.
    const toks = tokenizePathCandidates(child).filter((t) => confirmed.has(t.path))
    if (toks.length === 0) return <StreamText key={`st${i}`} text={child} />
    const parts: ReactNode[] = []
    let pos = 0
    for (let k = 0; k < toks.length; k++) {
      const t = toks[k]
      if (t.start > pos) {
        parts.push(<StreamText key={`p${i}.${k}`} text={child.slice(pos, t.start)} />)
      }
      parts.push(
        fileAnchor(
          { raw: child.slice(t.start, t.end), path: t.path, line: t.line },
          `a${i}.${k}`,
          onOpenPath,
        ),
      )
      pos = t.end
    }
    if (pos < child.length) {
      parts.push(<StreamText key={`p${i}.${toks.length}`} text={child.slice(pos)} />)
    }
    return parts
  })
}

/** Flatten a React children value (incl. multi-line plain text) to an array. */
function toArray(children: ReactNode): ReactNode[] {
  return Array.isArray(children) ? children.flat() : [children]
}

// --------------------------------------------------------------- streaming text

/**
 * How much of this span's text the DOM already holds. The text node is
 * grown only by appendData / rewritten only by the non-append branch
 * below, so this count plus the node's own data decide append vs rewrite.
 */
interface StreamState {
  committed: number
}

/**
 * Append-only rendering for text that grows while it is on screen (#275).
 *
 * The DOM Selection API anchors a selection in text NODES at character
 * offsets. Committing a changed string child rewrites the whole node
 * (`replaceData(0, len, text)`), and replace-data clamps any live Range
 * boundary INSIDE the replaced region back to the region start — the entire
 * old text is the replaced region, so every streamed delta collapsed the
 * reader's selection to a caret at the node start. That clamping is spec
 * behavior (dom: ~concept-cd-replace), identical in Blink — this was the
 * real, still-unfixed mechanism behind #275; stable keys alone (#281) could
 * not touch it.
 *
 * So React never owns this text. The span renders empty and this component
 * manages exactly one text node inside it:
 *  - a pure append (the streaming path) -> `appendData`: no boundary at or
 *    before the old end moves — a live drag end parked at the growing edge
 *    survives and new text lands AFTER it (no over-extension, AC 2);
 *  - a non-append (markdown syntax resolving mid-stream, shrink, say-tag
 *    strip) -> one whole-node rewrite of this line-span. Selections anchored
 *    in EARLIER nodes/paragraphs still survive untouched.
 *
 * useLayoutEffect keeps the DOM correct before paint (no flicker); the body
 * is idempotent, so StrictMode's double invoke is a no-op append of ''.
 */
function StreamText({ text }: { text: string }) {
  const spanRef = useRef<HTMLSpanElement>(null)
  const committedRef = useRef(0)

  useLayoutEffect(() => {
    const el = spanRef.current
    if (!el) return
    let node = el.firstChild as Text | null
    if (node === null) {
      node = document.createTextNode(text)
      el.appendChild(node)
      committedRef.current = text.length
      return
    }
    const committed = committedRef.current
    const current = node.nodeValue ?? ''
    const isAppend =
      text.length >= committed &&
      text.length >= current.length &&
      text.slice(0, committed) === current.slice(0, committed)
    if (isAppend) {
      // Only the never-committed suffix is new; append it in place. (Text
      // mutation is appendData — insertData would be wrong here: base is
      // always the node's current end in this branch.)
      const base = Math.max(committed, current.length)
      if (text.length > base) node.appendData(text.slice(base))
      committedRef.current = text.length
    } else {
      // Non-append rewrite: one replace of this line-span's text.
      node.nodeValue = text
      committedRef.current = text.length
    }
  })

  return <span data-stream-text="" ref={spanRef} />
}

// #275: module-level constants — stable identities across every render.
// Plain text containers linkify confirmed workspace paths (#258). `p`
// covers ordinary prose; `li` covers tight list items (their text is NOT
// wrapped in a paragraph when remark-gfm's list is tight). Styling stays
// the react-markdown defaults — chat rendering must not change visually.
const MdP = ({ children }: { children?: ReactNode }) => <p>{pathAware(children)}</p>
const MdLi = ({ children }: { children?: ReactNode }) => <li>{pathAware(children)}</li>

const MdA = ({ href, children }: { href?: string; children?: ReactNode }) => {
  const safe = safeHref(href)
  return safe ? (
    <a
      href={safe}
      target="_blank"
      rel="noreferrer"
      onClick={(e) => openExternal(safe, e)}
      className="text-sky-400 underline decoration-sky-400/40 underline-offset-2 hover:text-sky-300"
    >
      {streamOwnText(children)}
    </a>
  ) : (
    <span className="text-zinc-300">{streamOwnText(children)}</span>
  )
}

// Inline code renders as a chip. Fenced blocks are intercepted in `pre`
// below, so `code` here only ever sees inline spans.
const MdCode = ({ children }: { children?: ReactNode }) => (
  <code className="rounded bg-zinc-800 px-1 py-0.5 font-mono text-[12px] text-zinc-200">
    {streamOwnText(children)}
  </code>
)

// Fenced code block: extract text + language from the inner <code> element
// and hand it to the same CodeBlock the file views use. Works for fences
// with and without a language tag, and for unterminated fences mid-stream.
const MdPre = ({ children }: { children?: ReactNode }) => {
  const codeEl = Array.isArray(children) ? children[0] : children
  const props = (typeof codeEl === 'object' && codeEl !== null && 'props' in codeEl
    ? (codeEl as { props?: { className?: string } }).props
    : undefined) ?? {}
  const lang = /language-([\w+-]+)/.exec(props.className ?? '')?.[1]
  return <CodeBlock code={textOf(children)} lang={lang} />
}

const MdTable = ({ children }: { children?: ReactNode }) => (
  <div className="my-2 overflow-x-auto rounded border border-zinc-700">
    <table className="w-full border-collapse text-[13px]">{children}</table>
  </div>
)

const MdTd = ({ children }: { children?: ReactNode }) => (
  <td className="border-b border-zinc-800 px-2 py-1 align-top text-zinc-300">
    {pathAware(children)}
  </td>
)

/**
 * #275: inline containers (bold/italic/strike, inline code, link labels,
 * headings, table headers) also carry direct text that grows during a
 * stream — same replace-data hazard as paragraphs. Each maps STRING
 * children through StreamText and passes element children through.
 * Rendering parity: these reproduce react-markdown's default elements
 * exactly; linkification deliberately stays scoped to p/li/td (#258).
 */
function streamOwnText(children: ReactNode): ReactNode {
  return toArray(children).map((child, i) =>
    typeof child === 'string' && child.length > 0 ? (
      <StreamText key={`i${i}`} text={child} />
    ) : (
      child
    ),
  )
}

const MdStrong = ({ children }: { children?: ReactNode }) => (
  <strong>{streamOwnText(children)}</strong>
)
const MdEm = ({ children }: { children?: ReactNode }) => <em>{streamOwnText(children)}</em>
const MdDel = ({ children }: { children?: ReactNode }) => <del>{streamOwnText(children)}</del>
const MdThStream = ({ children }: { children?: ReactNode }) => (
  <th className="border-b border-zinc-700 bg-zinc-800/60 px-2 py-1 text-left font-medium text-zinc-200">
    {streamOwnText(children)}
  </th>
)
const MdH1 = ({ children }: { children?: ReactNode }) => <h1>{streamOwnText(children)}</h1>
const MdH2 = ({ children }: { children?: ReactNode }) => <h2>{streamOwnText(children)}</h2>
const MdH3 = ({ children }: { children?: ReactNode }) => <h3>{streamOwnText(children)}</h3>
const MdH4 = ({ children }: { children?: ReactNode }) => <h4>{streamOwnText(children)}</h4>
const MdH5 = ({ children }: { children?: ReactNode }) => <h5>{streamOwnText(children)}</h5>
const MdH6 = ({ children }: { children?: ReactNode }) => <h6>{streamOwnText(children)}</h6>

// Frozen at module load: the same component identities on every render of
// every message (#275). React reconciles by element-type identity, so these
// surviving identities keep the DOM nodes — and any live text selection —
// alive across streamed deltas.
const MARKDOWN_COMPONENTS = {
  p: MdP,
  li: MdLi,
  a: MdA,
  code: MdCode,
  pre: MdPre,
  table: MdTable,
  th: MdThStream,
  td: MdTd,
  strong: MdStrong,
  em: MdEm,
  del: MdDel,
  h1: MdH1,
  h2: MdH2,
  h3: MdH3,
  h4: MdH4,
  h5: MdH5,
  h6: MdH6,
}

/**
 * Full markdown rendering for agent messages. remark-breaks renders a
 * single \n as a line break (chat semantics) — the #17 emission separator
 * the stream writes is a bare \n and must stay visible.
 *
 * Plain text inside paragraphs, list items and table cells is additionally
 * linkified (#258): workspace-relative, existence-checked file paths (with
 * tolerated `:42` / `#L42` suffixes) open in the file preview on click.
 * Code spans and fenced blocks never reach the text renderer, so they stay
 * untouched; markdown link targets are never seen here either.
 */
export function AgentMarkdown({ content }: { content: string }) {
  const workspace = useAgent((s) => s.workspace)
  const setPreviewPath = useAgent((s) => s.setPreviewPath)
  const confirmed = usePathTokens(stripSay(content), workspace || null)
  const onOpenPath: PathOpener = (path) => setPreviewPath(path)
  const scope = { confirmed, onOpenPath }
  return (
    <MarkdownContext.Provider value={scope}>
      <div className="space-y-1 break-words [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
        <ReactMarkdown
          remarkPlugins={[remarkGfm, remarkBreaks]}
          components={MARKDOWN_COMPONENTS}
        >
          {stripSay(content)}
        </ReactMarkdown>
      </div>
    </MarkdownContext.Provider>
  )
}
