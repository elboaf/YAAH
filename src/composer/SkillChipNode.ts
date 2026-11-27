/** Skill chip inline node for the Lexical composer (#711, phase 2).
 *
 * A skill chip renders the same `$name` token the old canvas overlay painted
 * over — but now it is a real inline atomic node that deletes as a unit (one
 * backspace removes the whole chip) instead of a paint layer over literal text.
 *
 * Send-path invariant (the load-bearing choice): the editor serializes to
 * plain text that carries `$name` for every chip, so `deriveInvokedSkills`
 * (src/skillTokens.ts) keeps scanning it unchanged. The chip is a *view* of
 * the serialized text, never separate state. Editing raw text re-derives
 * chips; a chip inserted re-derives the text.
 */
import { ElementNode, NodeKey } from 'lexical'

export class SkillChipNode extends ElementNode {
  /** The invoked skill name. Public field so the serializer (which walks the
   *  raw node map) can read it as `node.skillName`. */
  skillName: string

  constructor(skillName: string = '', key?: NodeKey) {
    super(key)
    this.skillName = skillName
  }

  /** Static getType() — Lexical calls `SkillChipNode.getType()` at editor
   *  creation, so it MUST be static, not an instance method. */
  static getType(): string {
    return 'skillchip'
  }

  static clone(node: SkillChipNode): SkillChipNode {
    return new SkillChipNode(node.skillName, node.__key)
  }

  override isInline(): boolean {
    return true
  }

  override createDOM(): HTMLElement {
    const span = document.createElement('span')
    span.className = 'skill-chip'
    span.setAttribute('data-skill', this.skillName)
    span.setAttribute('contenteditable', 'false')
    span.setAttribute('role', 'button')
    span.setAttribute('title', `Remove skill ${this.skillName}`)
    // The label. Kept in a span so the chip can carry a remove affordance
    // (the ×) without changing what the send path scans.
    const label = document.createElement('span')
    label.className = 'skill-chip__label'
    label.textContent = `$${this.skillName}`
    span.append(label)
    // Remove affordance. Clicking it bubbles to the composer root, where the
    // click handler deletes the chip's `$name` token from the serialized text.
    const remove = document.createElement('span')
    remove.className = 'skill-chip__remove'
    remove.setAttribute('aria-label', `Remove skill ${this.skillName}`)
    remove.setAttribute('role', 'button')
    remove.textContent = '×'
    span.append(remove)
    return span
  }

  override updateDOM(): boolean {
    // When skillName changes, the element must be recreated; the chip's
    // serialized token and label both depend on the name.
    return false
  }

  override isEmpty(): boolean {
    return this.skillName.length === 0
  }

  /** The literal token the chip serializes to. The send path scans this. */
  getSerializedToken(): string {
    return `$${this.skillName}`
  }
}