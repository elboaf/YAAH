/** Lexical-backed composer component (#711, phase 2 skeleton).
 *
 * Replaces the textarea behind a single component boundary. The editor holds
 * plain text; chips are inline SkillChipNodes that serialize to `$name`
 * tokens, so the send path (deriveInvokedSkills) scans the same text YAAH
 * always sent. `value`/`onChange` keep the parent's `input` as the single
 * source of truth — no separate chip state.
 *
 * Controlled sync: the parent owns `value`. On `value` change we push it into
 * the editor via `editor.update` (guarded so the serialize listener does not
 * loop back to onChange). The editor's serialize-on-change notifies the
 * parent of user edits.
 */
import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'
import { LexicalComposer } from '@lexical/react/LexicalComposer'
import { RichTextPlugin } from '@lexical/react/LexicalRichTextPlugin'
import { HistoryPlugin } from '@lexical/react/LexicalHistoryPlugin'
import { ContentEditable } from '@lexical/react/LexicalContentEditable'
import { $getRoot, $createTextNode, $createParagraphNode, type LexicalEditor } from 'lexical'
import { createComposerEditor, composerEditableStyle, composerTheme, serializeEditorState } from './createComposerEditor'
import { SkillChipNode } from './SkillChipNode'
import { ComposerErrorBoundary } from './ComposerErrorBoundary'
import { SKILL_TOKEN_RE } from '../skillTokens'

export interface ComposerHandle {
  focus: () => void
}

export interface ComposerEditorProps {
  value: string
  onChange: (text: string) => void
  placeholder?: string
  disabled?: boolean
  onPasteFiles?: (files: FileList | File[]) => void
  onKeyDown?: (e: React.KeyboardEvent<HTMLDivElement>) => void
  className?: string
  /** Set of known skill names; `$name` tokens matching this set become chips. */
  knownSkills?: Set<string>
}

export const ComposerEditor = forwardRef<ComposerHandle, ComposerEditorProps>(
  ({ value, onChange, placeholder, disabled, onPasteFiles, onKeyDown, className, knownSkills }, ref) => {
    // Stable editor instance across renders. Created synchronously (outside
    // useEffect) so the Lexical editor renders on the first paint — the
    // fallback textarea only shows before the first render.
    const syncingRef = useRef(false)
    const editorRef = useRef<ReturnType<typeof createComposerEditor> | null>(null)
    if (editorRef.current === null) {
      editorRef.current = createComposerEditor((text) => {
        // Notify the parent of user edits (guarded against the sync loop).
        if (syncingRef.current) return
        onChange(text)
      })
    }

    // Push `value` into the editor whenever the parent changes it (dictation,
    // draft restore, skill pick, programmatic clear). Parse `$name` tokens
    // matching known skills into SkillChipNodes. Wrap content in a paragraph
    // — Lexical's root accepts element nodes, not bare text.
    useEffect(() => {
      const editor = editorRef.current
      if (!editor) return
      syncingRef.current = true
      editor.update(() => {
        const root = $getRoot()
        root.clear()
        if (!value) return
        const paragraph = $createParagraphNode()
        const matches = [...value.matchAll(SKILL_TOKEN_RE)]
        let cursor = 0
        for (const m of matches) {
          const tokenStart = m.index ?? 0
          const tokenText = m[0]
          const skillName = m[1]
          if (tokenStart > cursor) {
            paragraph.append($createTextNode(value.slice(cursor, tokenStart)))
          }
          if (knownSkills?.has(skillName)) {
            paragraph.append(new SkillChipNode(skillName))
          } else {
            paragraph.append($createTextNode(tokenText))
          }
          cursor = (m.index ?? 0) + tokenText.length
        }
        if (cursor < value.length) {
          paragraph.append($createTextNode(value.slice(cursor)))
        }
        root.append(paragraph)
      })
      syncingRef.current = false
    }, [value, editorRef, knownSkills])

    useImperativeHandle(
      ref,
      (): ComposerHandle => ({
        focus: () => editorRef.current?.getRootElement()?.focus(),
      }),
      [],
    )

    const editor = editorRef.current

    // Expose the editor on the contentEditable root so tests can drive it
    // through its state API (editor.update + $createTextNode) instead of
    // fireEvent.change, which throws on a contentEditable div.
    const editorRootRef = useRef<HTMLDivElement>(null)
    useEffect(() => {
      const el = editorRootRef.current
      if (el && editor) {
        (el as unknown as { __editor?: LexicalEditor }).__editor = editor
      } else if (el && !editor) {
        delete (el as unknown as { __editor?: LexicalEditor }).__editor
      }
    }, [editor])

    if (!editor) {
      // Render a fallback textarea until the editor mounts.
      return (
        <textarea
          className={className ?? 'block w-full resize-none bg-transparent px-3 py-2 text-sm text-zinc-100 focus:outline-none'}
          style={{ height: 'auto', minHeight: '3.25rem', maxHeight: '16rem', boxSizing: 'border-box' }}
          placeholder={placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      )
    }

    return (
      <LexicalComposer
        initialConfig={{
          namespace: 'YAAHComposer',
          theme: composerTheme,
          nodes: [SkillChipNode],
          editable: true,
          onError: (error) => {
            // eslint-disable-next-line no-console
            console.error('[composer] Lexical error', error)
          },
        }}
      >
        <RichTextPlugin
          contentEditable={
            <div
              className={className ?? ''}
              style={composerEditableStyle}
              aria-disabled={disabled}
              // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
              onKeyDown={(e) => onKeyDown?.(e)}
              onPaste={(e) => {
                const files = e.clipboardData?.files
                if (files?.length && onPasteFiles) {
                  e.preventDefault()
                  onPasteFiles(files)
                }
              }}
            >
              <ContentEditable
                ref={editorRootRef}
                role="textbox"
                aria-label="Message the agent"
                onChange={(e) => {
                  // Bridge the editor's text back to the parent. In the browser
                  // the value lives in `textContent`; tests drive the same path
                  // through a synthetic `target.value`, so read whichever is set.
                  const el = e.target as HTMLElement & { value?: string }
                  const next = el.value ?? el.textContent ?? ''
                  onChange(next)
                }}
              />
            </div>
          }
          placeholder={
            <div className="composer-placeholder px-0 text-sm text-zinc-500">{placeholder}</div>
          }
          ErrorBoundary={ComposerErrorBoundary}
        />
        <HistoryPlugin />
      </LexicalComposer>
    )
  },
)
