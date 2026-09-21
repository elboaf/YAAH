import { useAgent } from './store'

/**
 * Open a link in the OS default browser (#31, #59). The Tauri webview
 * swallows target="_blank" navigation, so under Tauri the click is
 * intercepted and the href handed to the Rust `open_external` command —
 * the same invoke path the update chip's open_releases_page uses, which
 * is proven to work in the packaged app. (The opener plugin's JS binding
 * failed there even with correct capabilities — the #59 toast proved the
 * failure sits in the JS->plugin IPC layer, so we bypass it entirely.)
 *
 * Plain-browser dev (vite) and the vitest DOM keep the native
 * <a target="_blank"> behavior.
 *
 * Callers must gate on the same scheme allowlist as before (safeHref:
 * https?/mailto) — this helper decides HOW to open, never WHETHER. The
 * Rust side re-checks the scheme allowlist itself (chat links are
 * model-generated), and failures surface with the raw error text so a
 * regression can never again be silent.
 */
export function openExternal(href: string, event: { preventDefault(): void }): void {
  // Re-checked at call time on purpose: under plain vite dev / vitest there
  // is no invoke target, so those surfaces keep native navigation.
  if (!('__TAURI_INTERNALS__' in window)) return
  event.preventDefault()
  void (async () => {
    try {
      const { invoke } = await import('@tauri-apps/api/core')
      await invoke('open_external', { url: href })
    } catch (e) {
      // No longer silent (#59): a failed open must be diagnosable from the
      // console and visible to the user — with the actual cause, not just
      // the URL.
      const detail = e instanceof Error ? e.message : String(e)
      console.error('[openExternal] failed to open', href, e)
      useAgent.getState().pushToast({
        kind: 'error',
        title: 'Could not open link',
        body: `${href.slice(0, 80)} — ${detail.slice(0, 120)}`,
      })
    }
  })()
}
