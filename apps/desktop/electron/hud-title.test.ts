import assert from 'node:assert/strict'

import { test } from 'vitest'

import { HUD_WINDOW_TITLE, wireHudWindowTitle } from './hud-title'

test('wireHudWindowTitle sets the distinct HUD title', () => {
  let title: string | null = null

  const win = {
    setTitle: (t: string) => {
      title = t
    },
    on: () => {}
  }

  wireHudWindowTitle(win)

  assert.equal(title, 'Hermes HUD')
  assert.equal(title, HUD_WINDOW_TITLE)
})

test('wireHudWindowTitle guards the title against the page overwriting it', () => {
  let handler: ((event: { preventDefault(): void }) => void) | null = null

  const win = {
    setTitle: () => {},
    on: (name: string, listener: (event: { preventDefault(): void }) => void) => {
      if (name === 'page-title-updated') {
        handler = listener
      }
    }
  }

  wireHudWindowTitle(win)

  let prevented = false
  handler?.({ preventDefault: () => (prevented = true) })
  assert.equal(prevented, true)
})
