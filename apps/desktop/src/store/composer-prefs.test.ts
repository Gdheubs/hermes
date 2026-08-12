import { afterEach, describe, expect, it } from 'vitest'

import { $composerEnterSends, applyComposerPrefsFromConfig } from './composer-prefs'

afterEach(() => $composerEnterSends.set(true))

describe('composer prefs from Hermes config', () => {
  it('enables multiline-first mode only for an explicit false value', () => {
    applyComposerPrefsFromConfig({ desktop: { composer: { enter_sends: false } } })

    expect($composerEnterSends.get()).toBe(false)
  })

  it('preserves the backward-compatible Enter-to-send default for missing or invalid values', () => {
    $composerEnterSends.set(false)
    applyComposerPrefsFromConfig({ desktop: { composer: { enter_sends: 'false' } } })

    expect($composerEnterSends.get()).toBe(true)

    $composerEnterSends.set(false)
    applyComposerPrefsFromConfig({})

    expect($composerEnterSends.get()).toBe(true)
  })
})
