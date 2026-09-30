/** A staged or structured text attachment (#142). Small files carry
 *  `content` and ride inline; larger ones carry `path` (workspace-relative,
 *  under .yaah-attachments/) after staging via POST /api/attachments. */
export interface Attachment {
  name: string
  content?: string
  path?: string
  /** Legacy field name for the staged path (pre-#142 composer state). */
  savedPath?: string
  size: number
}

export const INLINE_LIMIT_BYTES = 100_000

/** The exact legacy inline string an attachment contributes to model
 *  context (backend mirror: backend/agent/attachments.py). Kept for the
 *  golden fixture and for rendering old drafts; the send path no longer
 *  concatenates — attachments travel as structured data (#142). */
export const attachmentText = (a: Attachment): string => {
  const content = a.content
  if (content !== undefined) {
    return `\n\n--- attached file: ${a.name} ---\n\`\`\`\n${content}\n\`\`\``
  }
  const kb = Math.max(1, Math.round(a.size / 1_000))
  const path = a.path ?? a.savedPath ?? ''
  return `\n\n--- attached file: ${a.name} (${kb} KB) ---\nSaved to ${path} in the workspace. Read it with read_file (use offset/limit for large files).`
}
