// Issue #255: the model provider (z.ai / GLM) injects control text like
// `<system_warning>⚠️ CONTEXT LOW …</system_warning>` into the response
// stream near the context limit. When that text rides at the front of the
// first user message, the mechanical title slice captures it and the
// provider's control copy becomes the chat's name — permanently, because
// the backend auto-title never rewrites once the stored title differs from
// the first-message slice. This helper strips a leading run of
// `<system_*>…</system_*>` blocks so provider markup can never reach the
// title channel (frontend `createConversation` slice; the backend mirrors
// the same rule in `backend/agent/loop.py` before its compare).
//
// A leading `<system_x>` tag with no closing tag consumes the rest of the
// string: unclosed provider control text is control text to the end.

export function stripProviderMarkup(text: string): string {
  let t = text.replace(/^\s+/, '')
  for (;;) {
    const open = t.match(/^<system_(\w+)>/)
    if (!open) return t
    const tag = open[1]
    const closer = `</system_${tag}>`
    const close = t.indexOf(closer)
    if (close === -1) return ''
    t = t.slice(close + closer.length).replace(/^\s+/, '')
  }
}
