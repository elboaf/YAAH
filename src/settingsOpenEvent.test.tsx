// Issue #896: the #889 Settings refactor moved the page swap into App
// (ChatPanel ↔ SettingsView), but the Sidebar button kept writing a dead
// local showSettings and dispatching `settings-opened` — an event nothing
// listened for — so clicking Settings opened nothing.
//
// This test renders the real App and drives the same event the sidebar
// button dispatches: the Settings page must mount, and closing it must
// unmount it AND run the post-close refresh the old SettingsModal close
// ran (rail globals + a fresh model probe, #685 strip comment).
//
// Run: npx vitest run src/settingsOpenEvent.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    getConfig: vi.fn(async () => ({})),
    getGitInfo: vi.fn(async () => ({ info: {} })),
    listAvailableModels: vi.fn(async () => ({ providers: {}, active_provider: '' })),
    ttsStatus: vi.fn(async () => ({ available: false, tts_enabled: false })),
    listAgents: vi.fn(async () => []),
    listWorkspaces: vi.fn(async () => []),
    getConversation: vi.fn(async () => { throw new Error('unused') }),
    getMessages: vi.fn(async () => []),
  }
})

import App from './App'
import { useAgent } from './store'
import { listAvailableModels } from './api'

const listModelsMock = vi.mocked(listAvailableModels)

const openSettings = () =>
  act(() => window.dispatchEvent(new CustomEvent('settings-opened')))

describe('Settings page opens from the settings-opened event (#896)', () => {
  beforeEach(() => {
    useAgent.setState({
      conversationId: null,
      statusByConv: {},
      finishedByConv: {},
      activeRemoteKey: null,
      messagesByConv: {},
      objectiveByConv: {},
      todosByConv: {},
      errorByConv: {},
    })
    vi.clearAllMocks()
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    vi.restoreAllMocks()
  })

  it('mounts the Settings page when the event fires', async () => {
    render(<App />)
    // The page chrome title — SettingsView's own heading, not the sidebar row.
    expect(screen.queryByRole('heading', { name: 'Settings' })).toBeNull()
    openSettings()
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Settings' })).toBeTruthy(),
    )
    // The left nav rail is the page's identity.
    expect(screen.getByLabelText('Settings sections')).toBeTruthy()
  })

  it('closing the page unmounts it and runs the post-close refresh', async () => {
    const globalsSpy = vi.fn().mockResolvedValue(undefined)
    vi.spyOn(useAgent.getState(), 'refreshGlobals').mockImplementation(globalsSpy)
    render(<App />)
    openSettings()
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Settings' })).toBeTruthy(),
    )
    listModelsMock.mockClear()
    globalsSpy.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Close settings' }))
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Settings' })).toBeNull(),
    )
    expect(globalsSpy).toHaveBeenCalledTimes(1)
    expect(listModelsMock).toHaveBeenCalled()
  })
})
