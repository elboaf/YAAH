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
 *  `$<skillName>` tokens. Block elements are separated by newlines.
 *
 *  Lexical stores children as a singly linked list (`__first`/`__next`), not
 *  an array, so we walk that list. The old `__children`-based walk matched an
 *  array that never exists on the root and silently returned `''`. */
export function serializeEditorState(editorState: EditorState): string {
  const nodeMap = editorState._nodeMap
  const root = nodeMap.get('root') as any
  if (!root) return ''
  return serializeNode(root, nodeMap)
}

function isBlockNode(node: any): boolean {
  return node.__type === 'paragraph' || node.__type === 'root'
}

function serializeNode(node: any, nodeMap: Map<string, any>): string {
  if (!node) return ''
  // Atomic inline node: emit its literal token, do not recurse.
  if (node.__type === 'skillchip') {
    return `$${node.skillName ?? ''}`
  }
  // Leaf text node.
  if (node.__type === 'text') {
    return node.__text ?? ''
  }
  // Tab character.
  if (node.__type === 'tab') {
    return '\t'
  }
  // Line break within a paragraph.
  if (node.__type === 'linebreak') {
    return '\n'
  }
  // Container (paragraph, root): walk children; separate block children with newlines.
  let out = ''
  let cur = node.__first
  while (cur) {
    const child = nodeMap.get(cur)
    if (out && isBlockNode(child)) {
      out += '\n'
    }
    out += serializeNode(child, nodeMap)
    cur = child.__next
  }
  return out
}
