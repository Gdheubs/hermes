import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { registry } from '@/contrib/registry'
import { $activeGatewayProfile } from '@/store/profile'

import { __resetBackendSkinSync, ingestBackendSkin } from './backend-sync'
import { skinPref, ThemeProvider, useTheme } from './context'
import { DEFAULT_SKIN_NAME, nousTheme } from './presets'

// The live-authoring loop: Hermes writes/edits one skin file and every surface
// repaints. An in-place edit keeps the NAME — only the palette moves.
const bloomberg = (foreground: string) => ({
  name: 'bloomberg',
  colors: { background: '#000000', ui_text: foreground, ui_accent: '#ff8000' }
})

const pluginTheme = {
  ...nousTheme,
  name: 'plugin-neon',
  label: 'Plugin Neon',
  description: 'Runtime SDK test theme',
  colors: { ...nousTheme.colors, foreground: '#12ff99' }
}

const cssVar = (name: string) => window.document.documentElement.style.getPropertyValue(name)

function UnknownThemeButton() {
  const { setTheme } = useTheme()

  return <button onClick={() => setTheme('definitely-not-a-theme')}>Pick unknown</button>
}

let disposePluginTheme: (() => void) | null = null

beforeEach(() => {
  window.localStorage.clear()
  __resetBackendSkinSync()
  $activeGatewayProfile.set('default')
})

afterEach(() => {
  cleanup()
  disposePluginTheme?.()
  disposePluginTheme = null
  $activeGatewayProfile.set('default')
})

describe('ThemeProvider ← backend skin sync', () => {
  it('applies an activated backend skin', () => {
    render(
      <ThemeProvider>
        <div />
      </ThemeProvider>
    )

    act(() => ingestBackendSkin(bloomberg('#ff9f0a'), { apply: true }))

    expect(cssVar('--theme-foreground')).toBe('#ff9f0a')
    expect(cssVar('--theme-background-seed')).toBe('#000000')
  })

  it('repaints an in-place edit of the ACTIVE skin (same name, new palette)', () => {
    render(
      <ThemeProvider>
        <div />
      </ThemeProvider>
    )

    act(() => ingestBackendSkin(bloomberg('#ff9f0a'), { apply: true }))
    expect(cssVar('--theme-foreground')).toBe('#ff9f0a')

    // Recolor the same skin file. The same-name apply guard correctly no-ops
    // (protects manual desktop picks), so the repaint must come from the
    // registry update reaching the active theme derivation.
    act(() => ingestBackendSkin(bloomberg('#ff2d95'), { apply: true }))
    expect(cssVar('--theme-foreground')).toBe('#ff2d95')
  })

  it('does not repaint an edit to an INACTIVE skin', () => {
    render(
      <ThemeProvider>
        <div />
      </ThemeProvider>
    )

    act(() => ingestBackendSkin(bloomberg('#ff9f0a'), { apply: true }))

    // A different skin registered without apply (e.g. seeded on reconnect)
    // must not touch the painted theme.
    act(() =>
      ingestBackendSkin({ name: 'forest', colors: { background: '#001100', ui_text: '#66ff66' } }, { apply: false })
    )
    expect(cssVar('--theme-foreground')).toBe('#ff9f0a')
  })
})

describe('ThemeProvider late-bound persistence', () => {
  it('repaints a persisted SDK skin when the runtime plugin registers after boot', () => {
    window.localStorage.setItem('hermes-desktop-theme-v2', pluginTheme.name)

    render(
      <ThemeProvider>
        <div />
      </ThemeProvider>
    )

    // The unresolved stored name paints through the Nous fallback without being
    // erased, then the registry mutation below resolves it reactively.
    expect(skinPref.resolve('default')).toBe(pluginTheme.name)
    expect(cssVar('--theme-foreground')).toBe(nousTheme.colors.foreground)

    act(() => {
      disposePluginTheme = registry.register({
        area: 'themes',
        id: 'test:plugin-neon',
        source: 'plugin:test',
        data: pluginTheme
      })
    })

    expect(cssVar('--theme-foreground')).toBe('#12ff99')
  })

  it('keeps live theme selection strict even though persisted names are lenient', () => {
    const view = render(
      <ThemeProvider>
        <UnknownThemeButton />
      </ThemeProvider>
    )

    fireEvent.click(view.getByRole('button', { name: 'Pick unknown' }))

    expect(skinPref.resolve('default')).toBe(DEFAULT_SKIN_NAME)
    expect(window.localStorage.getItem('hermes-desktop-theme-v2')).toBe(DEFAULT_SKIN_NAME)
  })
})
