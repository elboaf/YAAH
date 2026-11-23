/** Lexical editor configuration for the YAAH composer (#711, phase 2).
 *
 * A single editor instance is created per ComposerEditor mount. It holds
 * plain text as the single source of truth: chips are inline SkillChipNodes
 * that serialize to `$name` tokens, so the send path (deriveInvokedSkills)
 * scans the same plain text YAAH always sent.
 *
 * Serialization walks the editor state's node map and concatenates each
 * TextNode's `__text` with each SkillChipNode's `$<skillName>` token.
 */
import {
  createEditor,
  type EditorState,
  type EditorThemeClasses,
  type LexicalEditor,
} from 'lexical'
import { SkillChipNode } from './SkillChipNode'

/** The editor theme. Only the editable container needs styling; chips render
 *  via their own component. Kept minimal — no formatting toolbar yet. */
export const composerTheme: EditorThemeClasses = {
  ltr: 'ltr',
  rtl: 'rtl',
  placeholder: 'composer-placeholder',
}

/** Textarea-equivalent styling for the editable surface. Matches the old
 *  textarea's size contract so the composer chrome is unchanged. */
export const composerEditableStyle: React.CSSProperties = {
  minHeight: '3.25rem',
  maxHeight: '16rem',
  overflowY: 'auto',
  padding: '0.75rem 0.75rem',
  fontSize: '0.875rem',
  color: '#e4e4e7', // zinc-200
  background: 'transparent',
  outline: 'none',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  caretColor: '#e4e4e7',
}

/** Build the editor. `onChange` is called with the serialized plain text on
 *  every text change (chips -> `$name`), so the parent learns the send-side
 *  text without a feedback loop. `onError` surfaces non-fatal editor errors. */
export function createComposerEditor(
  onChange: (text: string) => void,
  onError?: (error: unknown) => void,
): LexicalEditor {
  const editor = createEditor({
    namespace: 'YAAHComposer',
    theme: composerTheme,
    nodes: [SkillChipNode],
    editable: true,
    onError: (error: Error) => {
      // Composer errors are not fatal; surface once in dev.
      onError?.(error)
    },
  })
  // The text-content listener fires only when the text changes (not on cursor
  // moves), so it drives the parent without a selection-driven feedback loop.
  editor.registerTextContentListener(() => {
    // We still walk the node map to emit chip tokens, because the listener
    // collapses atomic nodes to their text content (a chip renders empty by
    // default).
    onChange(serializeEditorState(editor.getEditorState()))
  })
  return editor
}

/** Serialize an editor state to the plain text YAAH sends: walk the node map
 *  in document order, concatenating TextNode `__text` and SkillChipNode
 *  `$<skillName>` tokens. */
export function serializeEditorState(editorState: EditorState): string {
  const nodeMap = editorState._nodeMap
  const root = nodeMap.get('root') as any
  if (!root) return ''
  return serializeChildren(root.__children ?? [], nodeMap)
}

function serializeChildren(children: string[], nodeMap: Map<string, any>): string {
  const parts: string[] = []
  for (const key of children) {
    const node = nodeMap.get(key)
    if (!node) continue
    if (node.__type === 'skillchip') {
      parts.push(`$${node.skillName ?? ''}`)
    } else if (node.__type === 'text') {
      parts.push(node.__text ?? '')
    } else if (node.__type === 'paragraph') {
      parts.push(serializeChildren(node.__children ?? [], nodeMap))
    }
  }
  return parts.join('')
}
