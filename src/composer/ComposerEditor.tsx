/** Lexical-backed composer component (#711, phase 2).
 *
 * Replaces the textarea behind a single component boundary. The editor holds
 * plain text; chips are inline SkillChipNodes that serialize to `$name`
 * tokens, so the send path (deriveInvokedSkills) scans the same text YAAH
 * always sent. `value`/`onChange` keep the parent's `input` as the single
 * source of truth — no separate chip state.
 *
 * Controlled sync: the parent owns `value`. A child component inside
 * LexicalComposer obtains the rendered editor via useLexicalComposerContext,
 * syncs value changes in, and emits serialized text changes out.
 */
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef } from 'react'
import { LexicalComposer } from '@lexical/react/LexicalComposer'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import { RichTextPlugin } from '@lexical/react/LexicalRichTextPlugin'
import { HistoryPlugin } from '@lexical/react/LexicalHistoryPlugin'
import { ContentEditable } from '@lexical/react/LexicalContentEditable'
import { $getRoot, $createTextNode, $createParagraphNode, type LexicalEditor } from 'lexical'
import { composerEditableStyle, composerTheme, serializeEditorState } from './createComposerEditor'
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

/** Internal child that wires up value sync and change serialization. */
const ComposerInner = ({
  value,
  onChange,
  placeholder,
  disabled,
  onPasteFiles,
  onKeyDown,
  className,
  knownSkills,
  editorRootRef,
  syncingRef,
}: {
  value: string
  onChange: (text: string) => void
  placeholder?: string
  disabled?: boolean
  onPasteFiles?: (files: FileList | File[]) => void
  onKeyDown?: (e: React.KeyboardEvent<HTMLDivElement>) => void
  className?: string
  knownSkills?: Set<string>
  editorRootRef: React.RefObject<HTMLDivElement | null>
  syncingRef: React.MutableRefObject<boolean>
}) => {
  const [editor] = useLexicalComposerContext()

  // Push `value` into the editor whenever the parent changes it (dictation,
  // draft restore, skill pick, programmatic clear). Parse `$name` tokens
  // matching known skills into SkillChipNodes. Wrap content in a paragraph.
  useEffect(() => {
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
  }, [value, editor, knownSkills, syncingRef])

  // Serialize text changes back to the parent.
  useEffect(() => {
    return editor.registerTextContentListener(() => {
      if (syncingRef.current) return
      onChange(serializeEditorState(editor.getEditorState()))
    })
  }, [editor, onChange, syncingRef])

  // Expose the editor on the contentEditable root for test access.
  useEffect(() => {
    const el = editorRootRef.current
    if (el) {
      ;(el as unknown as { __editor?: LexicalEditor }).__editor = editor
    }
    return () => {
      const el = editorRootRef.current
      if (el) {
        delete (el as unknown as { __editor?: LexicalEditor }).__editor
      }
    }
  }, [editor, editorRootRef])

  return (
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
          />
        </div>
      }
      placeholder={
        <div className="composer-placeholder px-0 text-sm text-zinc-500">{placeholder}</div>
      }
      ErrorBoundary={ComposerErrorBoundary}
    />
  )
}

export const ComposerEditor = forwardRef<ComposerHandle, ComposerEditorProps>(
  ({ value, onChange, placeholder, disabled, onPasteFiles, onKeyDown, className, knownSkills }, ref) => {
    const syncingRef = useRef(false)
    const editorRootRef = useRef<HTMLDivElement>(null)

    const focusHandle = useCallback(() => {
      const el = editorRootRef.current
      if (el) {
        const editor = (el as unknown as { __editor?: LexicalEditor }).__editor
        editor?.getRootElement()?.focus()
      }
    }, [])

    useImperativeHandle(
      ref,
      (): ComposerHandle => ({
        focus: focusHandle,
      }),
      [focusHandle],
    )

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
        <ComposerInner
          value={value}
          onChange={onChange}
          placeholder={placeholder}
          disabled={disabled}
          onPasteFiles={onPasteFiles}
          onKeyDown={onKeyDown}
          className={className}
          knownSkills={knownSkills}
          editorRootRef={editorRootRef}
          syncingRef={syncingRef}
        />
        <HistoryPlugin />
      </LexicalComposer>
    )
  },
)