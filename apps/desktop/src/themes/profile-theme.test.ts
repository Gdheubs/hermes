import { beforeEach, describe, expect, it } from 'vitest'

import { modePref, skinPref } from './context'
import { DEFAULT_SKIN_NAME } from './presets'

// Skin and mode share one per-profile contract, so assert it once over both.
interface Pref {
  resolve: (profile: string) => string
  assign: (profile: string, value: string) => void
}

const cases = [
  {
    name: 'skin',
    pref: skinPref as unknown as Pref,
    fallback: DEFAULT_SKIN_NAME,
    a: 'ember',
    b: 'midnight'
  },
  { name: 'mode', pref: modePref as unknown as Pref, fallback: 'light', a: 'dark', b: 'system' }
]

describe.each(cases)('per-profile $name', ({ pref, fallback, a, b }) => {
  beforeEach(() => window.localStorage.clear())

  it('falls back to the default when unassigned', () => {
    expect(pref.resolve('default')).toBe(fallback)
    expect(pref.resolve('work')).toBe(fallback)
  })

  it('keeps each profile on its own value', () => {
    pref.assign('work', a)
    pref.assign('default', b)
    expect(pref.resolve('work')).toBe(a)
    expect(pref.resolve('default')).toBe(b)
  })

  it('lets unassigned profiles inherit the default profile as the global fallback', () => {
    pref.assign('default', a)
    expect(pref.resolve('never-themed')).toBe(a)
  })
})

describe('skin restart persistence', () => {
  beforeEach(() => window.localStorage.clear())

  it('preserves an unresolved global skin name for late backend registration', () => {
    window.localStorage.setItem('hermes-desktop-theme-v2', 'trt')

    expect(skinPref.resolve('default')).toBe('trt')
  })

  it('preserves an unresolved named-profile skin name for late SDK registration', () => {
    window.localStorage.setItem('hermes-desktop-profile-themes-v1', JSON.stringify({ work: 'plugin-neon' }))

    expect(skinPref.resolve('work')).toBe('plugin-neon')
  })

  it.each(['nous-light', 'default', 'gold'])('still migrates retired skin %s to the default', retired => {
    window.localStorage.setItem('hermes-desktop-theme-v2', retired)

    expect(skinPref.resolve('default')).toBe(DEFAULT_SKIN_NAME)
  })
})

describe('mode persistence validation', () => {
  beforeEach(() => window.localStorage.clear())

  it('still normalizes an unknown stored mode back to the default', () => {
    modePref.assign('work', 'dusk' as never)

    expect(modePref.resolve('work')).toBe('light')
  })
})
