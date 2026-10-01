// #207 — Voice-tab smoke: the two spoken-briefing toggles render with the
// spec's labels, and the in-chat option greys out when emissions are
// disabled. The card is exported for isolation (SandboxSettingsCard
// precedent); the Settings modal embeds it and wires the callbacks to its
// say_emissions / say_in_chat config drafts.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual }
})

import { SaySettingsCard } from './components'

afterEach(() => cleanup())

const disableBox = () => screen.getByRole('checkbox', { name: /Disable <say> emissions/i })
const showBox = () => screen.getByRole('checkbox', { name: /Show <say> emissions in chat/i })

describe('spoken-briefing toggles (#207)', () => {
  it('renders both checkboxes with defaults (emissions on, in-chat hidden)', () => {
    render(
      <SaySettingsCard
        sayEmissions
        sayInChat={false}
        onSayEmissions={() => {}}
        onSayInChat={() => {}}
      />,
    )
    expect(disableBox()).not.toBeChecked()
    expect(showBox()).not.toBeChecked()
    expect(showBox()).toBeEnabled()
  })

  it('unchecks + greys the in-chat option when emissions are disabled', () => {
    const state = { emissions: true, inChat: true }
    const props = () => ({
      sayEmissions: state.emissions,
      sayInChat: state.inChat,
      onSayEmissions: (on: boolean) => {
        state.emissions = on
      },
      onSayInChat: (on: boolean) => {
        state.inChat = on
      },
    })
    const { rerender } = render(<SaySettingsCard {...props()} />)
    fireEvent.click(disableBox())
    expect(state.emissions).toBe(false)
    expect(state.inChat).toBe(false) // cleared, not left stale-on
    rerender(<SaySettingsCard {...props()} />)
    expect(showBox()).toBeDisabled()
  })

  it('emits the in-chat toggle independently when emissions are on', () => {
    const state = { inChat: false }
    render(
      <SaySettingsCard
        sayEmissions
        sayInChat={false}
        onSayEmissions={() => {}}
        onSayInChat={(on) => {
          state.inChat = on
        }}
      />,
    )
    fireEvent.click(showBox())
    expect(state.inChat).toBe(true)
  })
})
