// Agent chat markdown rendering. react-markdown + remark-gfm, mapped onto the
// app's existing zinc design language. Code blocks reuse the same CodeBlock
// used by the file preview / file panel. No dangerouslySetInnerHTML anywhere:
// react-markdown builds elements, so untrusted content cannot inject markup.
// Links are additionally restricted to http/https/mailto schemes.

import { useState, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'
import { highlightLine } from './codeview'
import { openExternal } from './openExternal'
import { stripSay } from './speech'

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

const components = {
  a: ({ href, children }: { href?: string; children?: ReactNode }) => {
    const safe = safeHref(href)
    return safe ? (
      <a
        href={safe}
        target="_blank"
        rel="noreferrer"
        onClick={(e) => openExternal(safe, e)}
        className="text-sky-400 underline decoration-sky-400/40 underline-offset-2 hover:text-sky-300"
      >
        {children}
      </a>
    ) : (
      <span className="text-zinc-300">{children}</span>
    )
  },
  // Inline code renders as a chip. Fenced blocks are intercepted in `pre`
  // below, so `code` here only ever sees inline spans.
  code: ({ children }: { children?: ReactNode }) => (
    <code className="rounded bg-zinc-800 px-1 py-0.5 font-mono text-[12px] text-zinc-200">
      {children}
    </code>
  ),
  // Fenced code block: extract text + language from the inner <code> element
  // and hand it to the same CodeBlock the file views use. Works for fences
  // with and without a language tag, and for unterminated fences mid-stream.
  pre: ({ children }: { children?: ReactNode }) => {
    const codeEl = Array.isArray(children) ? children[0] : children
    const props = (typeof codeEl === 'object' && codeEl !== null && 'props' in codeEl
      ? (codeEl as { props?: { className?: string } }).props
      : undefined) ?? {}
    const lang = /language-([\w+-]+)/.exec(props.className ?? '')?.[1]
    return <CodeBlock code={textOf(children)} lang={lang} />
  },
  table: ({ node: _n, children }: { node?: unknown; children?: ReactNode }) => (
    <div className="my-2 overflow-x-auto rounded border border-zinc-700">
      <table className="w-full border-collapse text-[13px]">{children}</table>
    </div>
  ),
  th: ({ node: _n, children }: { node?: unknown; children?: ReactNode }) => (
    <th className="border-b border-zinc-700 bg-zinc-800/60 px-2 py-1 text-left font-medium text-zinc-200">
      {children}
    </th>
  ),
  td: ({ node: _n, children }: { node?: unknown; children?: ReactNode }) => (
    <td className="border-b border-zinc-800 px-2 py-1 align-top text-zinc-300">
      {children}
    </td>
  ),
}

/** Full markdown rendering for agent messages. remark-breaks renders a
 *  single \n as a line break (chat semantics) — the #17 emission separator
 *  the stream writes is a bare \n and must stay visible. */
export function AgentMarkdown({ content }: { content: string }) {
  return (
    <div className="space-y-1 break-words [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={components}>
        {stripSay(content)}
      </ReactMarkdown>
    </div>
  )
}
