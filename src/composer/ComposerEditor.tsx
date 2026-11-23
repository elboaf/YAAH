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
import { $getRoot, $createTextNode } from 'lexical'
import { createComposerEditor, composerEditableStyle, composerTheme, serializeEditorState } from './createComposerEditor'
import { ComposerErrorBoundary } from './ComposerErrorBoundary'

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
}

export const ComposerEditor = forwardRef<ComposerHandle, ComposerEditorProps>(
  ({ value, onChange, placeholder, disabled, onPasteFiles, onKeyDown, className }, ref) => {
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
    // draft restore, skill pick, programmatic clear). Guard the loop.
    useEffect(() => {
      const editor = editorRef.current
      if (!editor) return
      syncingRef.current = true
      editor.update(() => {
        const root = $getRoot()
        root.clear()
        if (value) {
          root.append($createTextNode(value))
        }
      })
      syncingRef.current = false
    }, [value, editorRef])

    useImperativeHandle(
      ref,
      (): ComposerHandle => ({
        focus: () => editorRef.current?.getRootElement()?.focus(),
      }),
      [],
    )

    const editor = editorRef.current

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
