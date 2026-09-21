import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  checkForRcUpdate,
  checkForUpdate,
  compareVersions,
  installerUrlFor,
  isRcVersion,
  tagToVersion,
} from './update'

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

describe('compareVersions — prerelease ordering (#62)', () => {
  it('a stable is newer than any rc of the same X.Y.Z', () => {
    expect(compareVersions('1.0.7', '1.0.7-rc.1')).toBeGreaterThan(0)
    expect(compareVersions('1.0.7', '1.0.7-rc.9')).toBeGreaterThan(0)
    expect(compareVersions('1.0.7-rc.1', '1.0.7')).toBeLessThan(0)
  })
  it('higher rc.N is newer', () => {
    expect(compareVersions('1.0.7-rc.2', '1.0.7-rc.1')).toBeGreaterThan(0)
    expect(compareVersions('1.0.7-rc.10', '1.0.7-rc.9')).toBeGreaterThan(0)
    expect(compareVersions('1.0.7-rc.1', '1.0.7-rc.2')).toBeLessThan(0)
  })
  it('rcs of different X.Y.Z order by the numeric core first', () => {
    expect(compareVersions('1.0.8-rc.1', '1.0.7')).toBeGreaterThan(0)
    expect(compareVersions('1.0.7-rc.1', '1.0.6')).toBeGreaterThan(0)
    expect(compareVersions('1.0.6', '1.0.7-rc.1')).toBeLessThan(0)
  })
  it('equal rc versions compare 0', () => {
    expect(compareVersions('1.0.7-rc.1', '1.0.7-rc.1')).toBe(0)
  })
  it('legacy junk comparison is unchanged', () => {
    expect(compareVersions('0.19.0', '0.19.0-beta')).toBe(0)
    expect(compareVersions('abc', '0.0.0')).toBe(0)
  })
})

describe('isRcVersion (#62)', () => {
  it('detects rc versions', () => {
    expect(isRcVersion('1.0.7-rc.1')).toBe(true)
    expect(isRcVersion('1.0.7-rc.12')).toBe(true)
  })
  it('rejects stable and junk versions', () => {
    expect(isRcVersion('1.0.7')).toBe(false)
    expect(isRcVersion('1.0.7-rc1')).toBe(false)
    expect(isRcVersion('1.0.7-beta.1')).toBe(false)
    expect(isRcVersion('')).toBe(false)
    expect(isRcVersion('garbage')).toBe(false)
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
    expect(tagToVersion('v1.2.3-rc.1')).toBeNull()
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

describe('checkForRcUpdate (#62)', () => {
  afterEach(() => vi.unstubAllGlobals())

  const rel = (tag: string) => ({
    tag_name: tag,
    html_url: `https://github.com/elboaf/YAAH/releases/tag/${tag}`,
  })

  it('an rc install is offered a newer rc', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify([rel('v1.0.7-rc.2'), rel('v1.0.7-rc.1')]), { status: 200 })),
    )
    expect(await checkForRcUpdate('1.0.7-rc.1')).toEqual({
      version: '1.0.7-rc.2',
      htmlUrl: 'https://github.com/elboaf/YAAH/releases/tag/v1.0.7-rc.2',
    })
  })

  it('an rc install is moved onto the stable when it lands', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify([rel('v1.0.7'), rel('v1.0.7-rc.2')]), { status: 200 })),
    )
    expect(await checkForRcUpdate('1.0.7-rc.2')).toEqual({
      version: '1.0.7',
      htmlUrl: 'https://github.com/elboaf/YAAH/releases/tag/v1.0.7',
    })
  })

  it('picks the version-wise newest even when /releases order disagrees', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify([rel('v1.0.7-rc.1'), rel('v1.0.7-rc.2')]), { status: 200 })),
    )
    expect((await checkForRcUpdate('1.0.7-rc.1'))?.version).toBe('1.0.7-rc.2')
  })

  it('returns null when up to date (newest rc already running)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify([rel('v1.0.7-rc.1')]), { status: 200 })),
    )
    expect(await checkForRcUpdate('1.0.7-rc.1')).toBeNull()
  })

  it('returns null when the stable is older than the running rc (rollback guard)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify([rel('v1.0.6')]), { status: 200 })),
    )
    expect(await checkForRcUpdate('1.0.7-rc.1')).toBeNull()
  })

  it('ignores unparseable tags and returns null when nothing parses', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify([rel('latest'), rel('v1.2.3-rc1')]), { status: 200 })),
    )
    expect(await checkForRcUpdate('1.0.7-rc.1')).toBeNull()
  })

  it('returns null on HTTP failure', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 403 })))
    expect(await checkForRcUpdate('1.0.7-rc.1')).toBeNull()
  })
})