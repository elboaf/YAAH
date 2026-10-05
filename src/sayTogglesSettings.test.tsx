// #207 — Voice-tab smoke: the two spoken-briefing toggles render with the
// spec's labels, and the in-chat option greys out when emissions are
// disabled. The card is exported for isolation (SandboxSettingsCard
// precedent); the Settings modal embeds it and wires the callbacks to its
// say_emissions / say_in_chat config drafts.
// #295 — the narrator-persona picker joins the card: fixed v1 enum
// (neutral | jester | elitest), disabled alongside the rest of the card
// when emissions are off, and reporting selections through onSayPersona.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual }
})

import { SAY_PERSONA_OPTIONS, SaySettingsCard } from './components'

afterEach(() => cleanup())

const disableBox = () => screen.getByRole('checkbox', { name: /Disable <say> emissions/i })
const showBox = () => screen.getByRole('checkbox', { name: /Show <say> emissions in chat/i })
const personaPicker = () => screen.getByRole('combobox', { name: /Narrator persona/i })

const baseProps = () => ({
  sayEmissions: true,
  sayInChat: false,
  sayPersona: 'neutral',
  onSayEmissions: () => {},
  onSayInChat: () => {},
  onSayPersona: () => {},
})

describe('spoken-briefing toggles (#207)', () => {
  it('renders both checkboxes with defaults (emissions on, in-chat hidden)', () => {
    render(<SaySettingsCard {...baseProps()} />)
    expect(disableBox()).not.toBeChecked()
    expect(showBox()).not.toBeChecked()
    expect(showBox()).toBeEnabled()
  })

  it('unchecks + greys the in-chat option when emissions are disabled', () => {
    const state = { emissions: true, inChat: true }
    const props = () => ({
      ...baseProps(),
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
        {...baseProps()}
        onSayInChat={(on) => {
          state.inChat = on
        }}
      />,
    )
    fireEvent.click(showBox())
    expect(state.inChat).toBe(true)
  })
})

describe('narrator persona picker (#295)', () => {
  it('ships the fixed v1 enum, neutral first as default', () => {
    expect(SAY_PERSONA_OPTIONS.map((p) => p.value)).toEqual([
      'neutral',
      'jester',
      'elitest',
    ])
  })

  it('renders the picker with the saved persona selected', () => {
    render(<SaySettingsCard {...baseProps()} sayPersona="jester" />)
    expect(personaPicker()).toHaveValue('jester')
  })

  it('falls back to neutral for an unknown saved value (server does the same)', () => {
    render(<SaySettingsCard {...baseProps()} sayPersona="pirate" />)
    expect(personaPicker()).toHaveValue('neutral')
  })

  it('reports persona selections through onSayPersona', () => {
    const state = { persona: 'neutral' }
    render(
      <SaySettingsCard
        {...baseProps()}
        onSayPersona={(p) => {
          state.persona = p
        }}
      />,
    )
    fireEvent.change(personaPicker(), { target: { value: 'elitest' } })
    expect(state.persona).toBe('elitest')
  })

  it('greys the picker out when emissions are disabled', () => {
    render(<SaySettingsCard {...baseProps()} sayEmissions={false} />)
    expect(personaPicker()).toBeDisabled()
  })
})
