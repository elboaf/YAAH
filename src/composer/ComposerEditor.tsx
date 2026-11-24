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
import { $getRoot, $createTextNode, $createParagraphNode, PASTE_COMMAND, type LexicalEditor, $isNodeSelection, $isRangeSelection, $getSelection, $setSelection, $createRangeSelection, $createPoint, COPY_COMMAND } from 'lexical'
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
  // Skip if the editor already contains the same serialized value, so that
  // the user's own typed chip tokens don't trigger a rebuild-and-reselect
  // loop from the parent's echoed value.
  useEffect(() => {
    // Serialize the current editor state and compare before rebuilding.
    // If the user typed `$skill` and it was already converted to a chip,
    // the serialized text will match `value` exactly — skip the rebuild.
    const current = serializeEditorState(editor.getEditorState())
    if (current === value) return
    syncingRef.current = true
    editor.update(() => {
      // Preserve the caret across the rebuild: `root.clear()` drops the
      // paragraph that holds the insertion point, so the caret would jump to
      // the start and later input would land after the wrong token. Record the
      // offset before clearing and restore it after appending the new content.
      const selection = $getSelection()
      let caretOffset = 0
      if ($isRangeSelection(selection) && selection.focus) {
        caretOffset = selection.focus.offset
      } else {
        caretOffset = current.length
      }
      const root = $getRoot()
      root.clear()
      if (!value) return
      const paragraph = $createParagraphNode()
      const matches = [...value.matchAll(SKILL_TOKEN_RE)]
      let pos = 0
      for (const m of matches) {
        const tokenStart = m.index ?? 0
        const tokenText = m[0]
        const skillName = m[1]
        if (tokenStart > pos) {
          paragraph.append($createTextNode(value.slice(pos, tokenStart)))
        }
        if (knownSkills?.has(skillName)) {
          paragraph.append(new SkillChipNode(skillName))
        } else {
          paragraph.append($createTextNode(tokenText))
        }
        pos = (m.index ?? 0) + tokenText.length
      }
      if (pos < value.length) {
        paragraph.append($createTextNode(value.slice(pos)))
      }
      root.append(paragraph)

      // Restore the caret: the rebuilt content's first leaf is the new focus.
      // Select it at the recorded offset so later input lands after the token.
      const leaf = paragraph.getFirstChild()
      if (leaf) {
        const sel = $createRangeSelection()
        sel.anchor = $createPoint(leaf.getKey(), caretOffset, 'text')
        sel.focus = $createPoint(leaf.getKey(), caretOffset, 'text')
        $setSelection(sel)
      }
    })
    syncingRef.current = false
  }, [value, editor, knownSkills, syncingRef])

  // Serialize text changes back to the parent. Use an update listener that
  // compares serialized output, because a chip-only change (e.g. swapping one
  // skill for another) does not change text content but must notify the parent.
  useEffect(() => {
    let previousText = serializeEditorState(editor.getEditorState())
    return editor.registerUpdateListener(() => {
      const currentText = serializeEditorState(editor.getEditorState())
      if (currentText !== previousText) {
        previousText = currentText
        if (syncingRef.current) return
        onChange(currentText)
      }
    })
  }, [editor, onChange, syncingRef])

  // Paste files via PASTE_COMMAND. When clipboard has files and a callback
  // is set, consume the command to prevent default paste; otherwise let
  // normal paste continue.
  useEffect(() => {
    return editor.registerCommand(
      PASTE_COMMAND,
      (event) => {
        if (!onPasteFiles) return false
        const ce = event as ClipboardEvent
        const files = ce.clipboardData?.files
        if (!files?.length) return false
        event.preventDefault()
        onPasteFiles(files)
        return true
      },
      0,
    )
  }, [editor, onPasteFiles])

  // Clipboard: convert selected chips to their literal `$name` tokens on copy.
  // Lexical's default range export skips inline element content, so chips would
  // otherwise be lost in clipboard text.
  useEffect(() => {
    return editor.registerCommand(
      COPY_COMMAND,
      (event) => {
        // Only handle real copy events with clipboard data; keyboard
        // shortcuts (Ctrl+C) are also dispatched through COPY_COMMAND but
        // without clipboardData, so we bail early.
        if (!event || typeof event.preventDefault !== 'function') {
          return false
        }
        const ce = event as ClipboardEvent
        if (!ce.clipboardData) return false
        editor.update(() => {
          const selection = $getSelection()
          if (!$isRangeSelection(selection) && !$isNodeSelection(selection)) {
            return
          }
          const nodes = selection.getNodes()
          let clipboardText = ''
          for (const node of nodes) {
            if (node instanceof SkillChipNode) {
              clipboardText += `$${node.skillName}`
            } else {
              clipboardText += node.getTextContent()
            }
          }
          ce.preventDefault()
          ce.clipboardData!.setData('text/plain', clipboardText)
        })
        return true
      },
      0,
    )
  }, [editor])

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