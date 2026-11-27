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
import { $getRoot, $createTextNode, $createParagraphNode, PASTE_COMMAND, $isElementNode, $isTextNode, type LexicalEditor, $isNodeSelection, $isRangeSelection, $getSelection, $setSelection, $createRangeSelection, $createPoint, COPY_COMMAND, $getNodeFromDOMNode, $getNodeByKey } from 'lexical'
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

/** Character offset of a point in the serialized-text coordinate space
 *  (the same space `serializeEditorState` produces). Used to carry the caret
 *  across the value-sync rebuild.
 */
function charOffsetForPoint(
  point: { key: string; offset: number; type: 'text' | 'element'; getNode: () => any },
): number {
  if (point.type === 'text') {
    const node = point.getNode()
    const parent = node.getParent()
    if (!parent) return 0
    let offset = 0
    let child = parent.getFirstChild()
    while (child) {
      if (child.getKey() === node.getKey()) {
        return offset + point.offset
      }
      offset += child.getTextContent().length
      child = child.getNextSibling()
    }
    return 0
  }
  // Element point: character offset = total text of children before the point
  // (element points sit BETWEEN children, not inside one).
  const parent = point.getNode()
  let offset = 0
  let child = parent.getFirstChild()
  let index = 0
  while (child) {
    if (index >= point.offset) break
    offset += child.getTextContent().length
    index++
    child = child.getNextSibling()
  }
  return offset
}

/** Restore a caret at the recorded character offset inside a freshly
 *  rebuilt paragraph. Walks the children in document order, tracking the
 *  character offset, and lands the caret in the leaf whose range contains
 *  it. Text points are created only for actual TextNodes (a chip/element
 *  node cannot host a text point); inline elements are restored as element
 *  points at the sibling boundary.
 */
function restoreCaret(
  paragraph: any,
  caretChar: number,
): void {
  let offset = 0
  let child = paragraph.getFirstChild()
  let index = 0
  while (child) {
    const size = child.getTextContent().length
    if (offset + size >= caretChar) {
      const local = caretChar - offset
      if ($isTextNode(child)) {
        const sel = $createRangeSelection()
        sel.anchor = $createPoint(child.getKey(), local, 'text')
        sel.focus = $createPoint(child.getKey(), local, 'text')
        $setSelection(sel)
        return
      }
      // Inline element (e.g. chip): restore as an element point at the
      // sibling boundary (after this element).
      const sel = $createRangeSelection()
      sel.anchor = $createPoint(paragraph.getKey(), index + 1, 'element')
      sel.focus = $createPoint(paragraph.getKey(), index + 1, 'element')
      $setSelection(sel)
      return
    }
    offset += size
    index++
    child = child.getNextSibling()
  }
  // Caret is past the end of the content: restore at the very end.
  const last = paragraph.getLastChild()
  if (last && $isTextNode(last)) {
    const local = last.getTextContent().length
    const sel = $createRangeSelection()
    sel.anchor = $createPoint(last.getKey(), local, 'text')
    sel.focus = $createPoint(last.getKey(), local, 'text')
    $setSelection(sel)
    return
  }
  const sel = $createRangeSelection()
  const selIdx = last ? index : 0
  sel.anchor = $createPoint(paragraph.getKey(), selIdx, 'element')
  sel.focus = $createPoint(paragraph.getKey(), selIdx, 'element')
  $setSelection(sel)
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
    // Serialized text is identical whether a `$name` token is a chip or a
    // plain TextNode, so an exact string match does not prove the editor is
    // in the desired state. Rebuild when the editor still holds a
    // known-skill token as literal text (typed, or restored from a draft
    // whose chips serialized back to `$name`). Without this, a token typed
    // before skills load, or restored before skills load, never becomes a
    // chip — it stays as spell-checked `$name` text.
    const hasUnchippedToken = editor.getEditorState().read(() => {
      const root = $getRoot()
      let dirty = false
      const children = root.getChildren()
      children.forEach((node) => {
        if ($isElementNode(node)) {
          node.getChildren().forEach((child) => {
            if ($isTextNode(child)) {
              const text = child.getTextContent()
              for (const m of text.matchAll(SKILL_TOKEN_RE)) {
                if (knownSkills?.has(m[1])) {
                  dirty = true
                  return
                }
              }
            }
          })
        }
      })
      return dirty
    })
    if (current === value && !hasUnchippedToken) return
    syncingRef.current = true
    editor.update(() => {
      // Preserve the caret across the rebuild. `root.clear()` drops the
      // paragraph holding the insertion point, so the caret would jump to
      // the start and later input would land after the wrong token.
      //
      // The caret is recorded as a CHARACTER OFFSET in the serialized text
      // (the same coordinate space `serializeEditorState` produces), not a
      // local offset within a leaf. This is the one space the rebuild can
      // reproduce exactly: a token that becomes a chip changes leaf shape
      // but not its serialized length, so the character offset survives the
      // rebuild byte-identically.
      const selection = $getSelection()
      let caretChar = current.length
      if ($isRangeSelection(selection) && selection.focus) {
        caretChar = charOffsetForPoint(selection.focus)
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

      // Restore the caret at the recorded character offset. Walk the rebuilt
      // paragraph's children in document order, tracking the character offset,
      // and land the caret in the leaf whose range contains it. Text points
      // are created only for actual TextNodes; element points beside chips
      // are created with type 'element' at the sibling boundary.
      restoreCaret(paragraph, caretChar)
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

  // Paste files via a native listener on the contentEditable root. This
  // bypasses Lexical's command chain: registerRichText registers its own
  // PASTE_COMMAND at COMMAND_PRIORITY_EDITOR (100) that consumes clipboard
  // files and returns true, which stops the chain before any lower-priority
  // handler runs. The old textarea handled paste directly on the DOM, so we
  // restore that path here instead of relying on the command chain.
  useEffect(() => {
    const root = editorRootRef.current
    if (!root || !onPasteFiles) return
    const onPaste = (e: ClipboardEvent) => {
      const files = e.clipboardData?.files
      if (!files?.length) return
      e.preventDefault()
      onPasteFiles(files)
    }
    root.addEventListener('paste', onPaste)
    return () => root.removeEventListener('paste', onPaste)
  }, [editorRootRef, onPasteFiles])

  // Remove a skill chip when its pill is clicked. The composer is controlled
  // (the parent owns `value`), so removal is expressed by deleting the chip's
  // `$name` token from the serialized text and letting the value-sync effect
  // rebuild without it. This works even for a chip backspace cannot reach: an
  // inline element node is not atom-deletable, so one backspace leaves it
  // put. Clicking the pill's × (or the pill itself) is the reliable removal.
  //
  // The click is resolved to the Lexical node behind the clicked DOM element,
  // and that specific node is removed (not the first `$name` token in the
  // serialized text). A regex on the text would match the prefix of a
  // differently-named sibling chip (`$ask-matt $ask` -> `-matt $ask`) and
  // always remove the first occurrence, so duplicate-named chips could never
  // be removed in turn.
  useEffect(() => {
    const root = editorRootRef.current
    if (!root) return
    const onChipClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null
      const chip = target?.closest?.('.skill-chip') as HTMLElement | null
      if (!chip) return
      // Resolve the clicked chip to its Lexical node and remove just that one.
      // `editor.read` puts a callback in the active-editor context that
      // `$getNodeFromDOMNode` needs.
      const node = editor.read(() => $getNodeFromDOMNode(chip))
      editor.update(() => {
        if (node && $getNodeByKey(node.getKey()) === node) {
          node.remove()
        }
      })
      e.stopPropagation()
      e.preventDefault()
    }
    root.addEventListener('click', onChipClick)
    return () => root.removeEventListener('click', onChipClick)
  }, [editor, editorRootRef])

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
            style={{ outline: 'none' }}
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