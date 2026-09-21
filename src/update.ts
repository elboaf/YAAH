// In-app update check (#16): compare the running version against the latest
// GitHub release tag. The decision to actually install lives in Rust
// (download_installer + prepare_update handoff shim); this module is the
// pure detection + orchestration layer so it stays unit-testable.
import { useCallback, useEffect, useRef, useState } from 'react'
import { getVersion } from '@tauri-apps/api/app'
import { getCurrentWindow } from '@tauri-apps/api/window'
import { IS_TAURI } from './api'

/** Latest-release info worth surfacing, or null when up to date / unsure. */
export interface UpdateInfo {
  version: string
  htmlUrl: string
}

/** Chip lifecycle: quiet -> update found -> downloading -> handing off. */
export type UpdatePhase = 'idle' | 'ready' | 'downloading' | 'installing' | 'error'

/**
 * Prerelease-aware semver comparison (#62): major.minor.patch, then the
 * -rc.N suffix. An RC is older than its stable (1.0.7 > 1.0.7-rc.1) and a
 * higher rc.N is newer than a lower one (1.0.7-rc.2 > 1.0.7-rc.1).
 * Unparseable versions fall back to the legacy numeric-prefix comparison
 * (junk segments as 0 — a garbage tag must never produce a false
 * "update available").
 */
export function compareVersions(a: string, b: string): number {
  const parse = (v: string) => {
    const m = /^(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?$/.exec(v.trim())
    return m
      ? { maj: +m[1], min: +m[2], pat: +m[3], rc: m[4] === undefined ? null : +m[4] }
      : null
  }
  const pa = parse(a)
  const pb = parse(b)
  if (!pa || !pb) {
    // Legacy path (pre-#62 behavior): numeric prefix only, junk as 0.
    const na = a.split('.').map((p) => parseInt(p, 10) || 0)
    const nb = b.split('.').map((p) => parseInt(p, 10) || 0)
    for (let i = 0; i < 3; i++) {
      const d = (na[i] || 0) - (nb[i] || 0)
      if (d !== 0) return d
    }
    return 0
  }
  for (const k of ['maj', 'min', 'pat'] as const) {
    if (pa[k] !== pb[k]) return pa[k] - pb[k]
  }
  if (pa.rc === null && pb.rc === null) return 0
  if (pa.rc === null) return 1 // stable > any rc of the same X.Y.Z
  if (pb.rc === null) return -1
  return pa.rc - pb.rc
}

/** "1.0.7-rc.2" -> true; stable and junk versions -> false (#62). */
export function isRcVersion(version: string): boolean {
  return /^\d+\.\d+\.\d+-rc\.\d+$/.test(version.trim())
}

/** "v0.20.1" -> "0.20.1"; anything without a numeric core stays null. */
export function tagToVersion(tag: string): string | null {
  const m = /^v?(\d+\.\d+\.\d+)$/.exec(tag.trim())
  return m ? m[1] : null
}

/**
 * RC-tolerant variant used only by the RC channel check (#62): also accepts
 * `-rc.N` tags, which the strict parser must keep rejecting so a stable
 * install can never be offered a pre-release.
 */
export function tagToVersionWithRc(tag: string): string | null {
  const m = /^v?(\d+\.\d+\.\d+(?:-rc\.\d+)?)$/.exec(tag.trim())
  return m ? m[1] : null
}

/**
 * Check GitHub's latest stable release. Returns null when: not running in
 * Tauri (browser tab / remote session), the request fails (offline, rate
 * limit), the tag is unparseable, or the release is not newer than us.
 * Never throws — a failed check must not nag or break the sidebar.
 */
/** Cap a single check (#36) so a hung connection can't stall the poll loop. */
const CHECK_TIMEOUT_MS = 300 * 1000

export async function checkForUpdate(currentVersion: string): Promise<UpdateInfo | null> {
  if (!IS_TAURI) return null
  try {
    const res = await fetch('https://api.github.com/repos/elboaf/YAAH/releases/latest', {
      headers: { Accept: 'application/vnd.github+json' },
      // Timed-out checks surface as AbortError -> caught below -> null.
      signal: AbortSignal.timeout(CHECK_TIMEOUT_MS),
    })
    if (!res.ok) return null
    const body = (await res.json()) as { tag_name?: string; html_url?: string }
    const version = body.tag_name ? tagToVersion(body.tag_name) : null
    if (!version) return null
    if (compareVersions(version, currentVersion) <= 0) return null
    return { version, htmlUrl: body.html_url ?? 'https://github.com/elboaf/YAAH/releases/latest' }
  } catch {
    return null
  }
}

/**
 * RC-aware check for installs running an -rc.N version (#62): the newest
 * entry across stable AND pre-releases, prerelease-ordered. An RC install
 * follows rc.1 -> rc.2 -> ... and is moved onto the stable when it lands;
 * a stable install never takes this path (releases/latest already excludes
 * pre-releases, and tagToVersion keeps rejecting -rc tags — the core #62
 * guarantee is those two layers, not this function).
 */
export async function checkForRcUpdate(currentVersion: string): Promise<UpdateInfo | null> {
  if (!IS_TAURI) return null
  try {
    const res = await fetch('https://api.github.com/repos/elboaf/YAAH/releases?per_page=10', {
      headers: { Accept: 'application/vnd.github+json' },
      signal: AbortSignal.timeout(CHECK_TIMEOUT_MS),
    })
    if (!res.ok) return null
    const body = (await res.json()) as Array<{ tag_name?: string; html_url?: string }>
    // /releases is newest-first by created date, not version order — an
    // out-of-order stable bump could sit below an older rc. Pick the
    // version-wise newest parseable entry instead of trusting position.
    let best: { version: string; htmlUrl: string } | null = null
    for (const rel of body) {
      const version = rel.tag_name ? tagToVersionWithRc(rel.tag_name) : null
      if (!version) continue
      const htmlUrl = rel.html_url ?? `https://github.com/elboaf/YAAH/releases/tag/${rel.tag_name}`
      if (!best || compareVersions(version, best.version) > 0) best = { version, htmlUrl }
    }
    if (!best) return null
    if (compareVersions(best.version, currentVersion) <= 0) return null
    return best
  } catch {
    return null
  }
}

// ---------------------------------------------------------------- install flow

const DESKTOP_ASSET = 'yaah-desktop-setup.exe'
const RELEASES_BASE = 'https://github.com/elboaf/YAAH/releases'
const CHECK_INTERVAL_MS = 5 * 60 * 1000
const DOWNLOAD_TIMEOUT_MS = 10 * 60 * 1000

/**
 * Derive the desktop installer's download URL from the release tag — the
 * release.yml asset names are fixed (`yaah-desktop-setup.exe`), and the
 * `<tag>/download/<asset>` path serves the current asset for that tag even
 * if a later release supersedes it while we're mid-download.
 */
export function installerUrlFor(tag: string): string {
  return `${RELEASES_BASE}/download/${encodeURIComponent(tag)}/${DESKTOP_ASSET}`
}

/** True on Windows (Tauri) — the only platform with the installer handoff. */
export function isWindowsPlatform(): boolean {
  return navigator.userAgent.includes('Windows')
}

/**
 * Orchestration hook behind the sidebar chip. Mounts one poll loop (on
 * mount, then every 5 min; errors leave the chip hidden — never nag), and
 * exposes the click-to-install action with live progress.
 */
export function useUpdateCheck(): {
  update: UpdateInfo | null
  phase: UpdatePhase
  progress: number // 0..1, only meaningful while downloading
  error: string | null
  install: () => void
  openReleases: () => void
} {
  const [update, setUpdate] = useState<UpdateInfo | null>(null)
  const [phase, setPhase] = useState<UpdatePhase>('idle')
  const [progress, setProgress] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const phaseRef = useRef<UpdatePhase>('idle')
  const busyRef = useRef(false)
  const watchdogRef = useRef<number | undefined>(undefined)
  const setPhaseBoth = (p: UpdatePhase) => {
    phaseRef.current = p
    setPhase(p)
  }

  useEffect(() => {
    if (!IS_TAURI) return
    let cancelled = false
    let timer: number | undefined
    const tick = async () => {
      const cur = await getVersion().catch(() => null)
      if (!cur || cancelled) return
      // #62: an install running an -rc.N version follows the RC channel
      // (newest across stable + pre-releases); stable installs keep the
      // releases/latest check, which never sees an RC.
      const found = isRcVersion(cur) ? await checkForRcUpdate(cur) : await checkForUpdate(cur)
      if (cancelled) return
      setUpdate(found)
      // Phase transitions only across idle<->ready so a re-check can never
      // clobber an in-flight download or an error awaiting retry.
      if (found && phaseRef.current === 'idle') setPhaseBoth('ready')
      if (!found && phaseRef.current === 'ready') setPhaseBoth('idle')
    }
    void tick()
    timer = window.setInterval(tick, CHECK_INTERVAL_MS)
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearInterval(timer)
      // A lost download race (destroy during in-flight event) must not
      // wedge the phase; fresh mounts start clean anyway.
      phaseRef.current = 'idle'
    }
  }, [])

  useEffect(() => {
    // Download progress rides the Rust-emitted `update-progress` event (the
    // download thread is Rust-side, so it can't resolve the invoke).
    if (!IS_TAURI) return
    const onProgress = (e: Event) => {
      const d = (e as CustomEvent<{ state: string; received?: number; total?: number; path?: string; message?: string }>)
        .detail
      if (phaseRef.current !== 'downloading') return
      if (d.state === 'downloading' && d.total) setProgress(Math.min(1, (d.received ?? 0) / d.total))
      if (d.state === 'done') {
        window.clearTimeout(watchdogRef.current)
        setPhaseBoth('installing')
        void beginHandoff(d.path ?? '')
      }
      if (d.state === 'error') {
        window.clearTimeout(watchdogRef.current)
        setError(d.message ?? 'download failed')
        setPhaseBoth('error')
        busyRef.current = false
      }
    }
    window.addEventListener('update-progress', onProgress)
    return () => window.removeEventListener('update-progress', onProgress)
  }, [])

  const beginHandoff = async (path: string) => {
    try {
      const { invoke } = await import('@tauri-apps/api/core')
      await invoke('prepare_update', { installerPath: path })
      // Handoff armed: the shim is waiting for our PID. Destroying the
      // window triggers the shell's normal shutdown (backend kill_tree
      // included) — same path as the user closing the app.
      await getCurrentWindow().destroy()
    } catch (e) {
      setError(String(e))
      setPhaseBoth('error')
      busyRef.current = false
    }
  }

  const install = useCallback(() => {
    // 'error' stays clickable — clicking retries the download.
    if (!update || busyRef.current) return
    if (phaseRef.current === 'downloading' || phaseRef.current === 'installing') return
    if (!isWindowsPlatform()) {
      // Linux has no installer handoff in this iteration: releases page.
      window.open(update.htmlUrl, '_blank')
      return
    }
    busyRef.current = true
    setError(null)
    setProgress(0)
    setPhaseBoth('downloading')
    // The invoke resolves as soon as the download STARTS (progress streams
    // via update-progress events), so completion/failure watchdogging
    // happens on the event path — this timer catches the silent case.
    watchdogRef.current = window.setTimeout(() => {
      if (phaseRef.current === 'downloading') {
        setError('download timed out')
        setPhaseBoth('error')
        busyRef.current = false
      }
    }, DOWNLOAD_TIMEOUT_MS)
    void (async () => {
      try {
        const { invoke } = await import('@tauri-apps/api/core')
        const tag = `v${update.version}`
        await invoke<string>('download_installer', { url: installerUrlFor(tag) })
      } catch (e) {
        window.clearTimeout(watchdogRef.current)
        setError(String(e))
        setPhaseBoth('error')
        busyRef.current = false
      }
    })()
  }, [update])

  const openReleases = useCallback(() => {
    const url = update?.htmlUrl ?? `${RELEASES_BASE}/latest`
    if (IS_TAURI) {
      void (async () => {
        try {
          const { invoke } = await import('@tauri-apps/api/core')
          await invoke('open_releases_page', { url })
        } catch {
          window.open(url, '_blank')
        }
      })()
    } else {
      window.open(url, '_blank')
    }
  }, [update])

  return { update, phase, progress, error, install, openReleases }
}
