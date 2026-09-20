import { afterEach, describe, expect, it, vi } from 'vitest'
import { checkForUpdate, compareVersions, installerUrlFor, tagToVersion } from './update'

// update.ts gates everything on IS_TAURI (from ./api). Mock it true so the
// fetch path is reachable; the false case is covered by the first test.
vi.mock('./api', () => ({ IS_TAURI: true }))

describe('compareVersions', () => {
  it('orders major/minor/patch', () => {
    expect(compareVersions('0.20.0', '0.19.0')).toBeGreaterThan(0)
    expect(compareVersions('0.20.1', '0.20.0')).toBeGreaterThan(0)
    expect(compareVersions('1.0.0', '0.99.99')).toBeGreaterThan(0)
    expect(compareVersions('0.19.0', '0.20.0')).toBeLessThan(0)
  })
  it('equal versions compare 0', () => {
    expect(compareVersions('0.19.0', '0.19.0')).toBe(0)
  })
  it('tolerates junk segments as 0 (never false-positives)', () => {
    expect(compareVersions('0.19.0', '0.19.0-beta')).toBe(0)
    expect(compareVersions('abc', '0.0.0')).toBe(0)
  })
})

describe('tagToVersion', () => {
  it('strips the v prefix', () => {
    expect(tagToVersion('v0.20.1')).toBe('0.20.1')
    expect(tagToVersion('0.20.1')).toBe('0.20.1')
  })
  it('rejects unparseable tags (conservative: no chip)', () => {
    expect(tagToVersion('latest')).toBeNull()
    expect(tagToVersion('v1.2')).toBeNull()
    expect(tagToVersion('v1.2.3-rc1')).toBeNull()
    expect(tagToVersion('')).toBeNull()
  })
})

describe('installerUrlFor', () => {
  it('points at the fixed release.yml asset name for the tag', () => {
    expect(installerUrlFor('v0.20.1')).toBe(
      'https://github.com/elboaf/YAAH/releases/download/v0.20.1/yaah-desktop-setup.exe',
    )
  })
})

describe('checkForUpdate', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('returns the release when newer than the running version', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ tag_name: 'v0.20.0', html_url: 'https://github.com/elboaf/YAAH/releases/tag/v0.20.0' }), { status: 200 }),
      ),
    )
    expect(await checkForUpdate('0.19.0')).toEqual({
      version: '0.20.0',
      htmlUrl: 'https://github.com/elboaf/YAAH/releases/tag/v0.20.0',
    })
  })

  it('returns null when up to date', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ tag_name: 'v0.19.0' }), { status: 200 })),
    )
    expect(await checkForUpdate('0.19.0')).toBeNull()
  })

  it('returns null on HTTP failure (rate limit, offline CDN)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 403 })))
    expect(await checkForUpdate('0.19.0')).toBeNull()
  })

  it('returns null on a garbage tag', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ tag_name: 'not-a-version' }), { status: 200 })),
    )
    expect(await checkForUpdate('0.19.0')).toBeNull()
  })

  it('returns null when fetch throws (no network)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline') }))
    expect(await checkForUpdate('0.19.0')).toBeNull()
  })

  // #36: the check is capped (300s) via AbortSignal.timeout -> fetch. A hung
  // connection must surface as a rejected (aborted) fetch -> null, never a
  // forever-pending promise that stalls the poll loop. The native timer behind
  // AbortSignal.timeout ignores fake timers, so we pin the wiring (300s) and
  // fire the abort ourselves — what the real timer would do.
  it('caps the request at 300s and resolves null when it fires (#36)', async () => {
    const controller = new AbortController()
    const timeoutSpy = vi.spyOn(AbortSignal, 'timeout').mockImplementation(() => controller.signal)
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, init?: { signal?: AbortSignal }) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () =>
            reject(new DOMException('The operation was aborted.', 'AbortError')),
          )
        }),
      ),
    )
    try {
      const pending = checkForUpdate('0.19.0')
      expect(timeoutSpy).toHaveBeenCalledWith(300 * 1000)
      controller.abort()
      expect(await pending).toBeNull()
    } finally {
      timeoutSpy.mockRestore()
    }
  })

  it('passes a timeout signal on every check request (#36)', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: { signal?: AbortSignal }) =>
        new Response(JSON.stringify({ tag_name: 'v0.19.0' }), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    await checkForUpdate('0.19.0')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const init = fetchMock.mock.calls[0][1] as { signal?: AbortSignal }
    expect(init?.signal).toBeInstanceOf(AbortSignal)
    expect(init?.signal?.aborted).toBe(false)
  })
})
