import { openUrl } from '@tauri-apps/plugin-opener'
import { useAgent } from './store'

/**
 * Open a link in the OS default browser (#31). The Tauri webview swallows
 * target="_blank" navigation, so under Tauri the click is intercepted and
 * the href handed to tauri-plugin-opener; plain-browser dev (vite) and the
 * vitest DOM keep the native <a target="_blank"> behavior.
 *
 * Callers must gate on the same scheme allowlist as before (safeHref:
 * https?/mailto) — this helper decides HOW to open, never WHETHER.
 *
 * Issue #59: the previous version gated on IS_TAURI at module-eval time,
 * dynamically imported the plugin glue, and swallowed every failure with
 * `.catch(() => {})` — so a misfired gate or a failed import died silently
 * and clicking a link did nothing. Now the Tauri check happens at call time
 * (module eval order can't bite it), the import is static, and failures log
 * and toast instead of vanishing.
 */
export function openExternal(href: string, event: { preventDefault(): void }): void {
  // Re-checked at call time on purpose: under plain vite dev / vitest there
  // is no opener plugin to call, so those surfaces keep native navigation.
  if (!('__TAURI_INTERNALS__' in window)) return
  event.preventDefault()
  openUrl(href).catch((e: unknown) => {
    // No longer silent (#59): a failed open must be diagnosable from the
    // console and visible to the user.
    console.error('[openExternal] failed to open', href, e)
    useAgent.getState().pushToast({
      kind: 'error',
      title: 'Could not open link',
      body: href.slice(0, 120),
    })
  })
}
