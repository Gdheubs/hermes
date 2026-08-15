import assert from 'node:assert/strict'

import { test } from 'vitest'

import { defaultHudBounds } from './hud-geometry'

test('defaultHudBounds restores the standard centered bottom layout', () => {
  assert.deepEqual(defaultHudBounds({ x: 0, y: 25, width: 1440, height: 875 }), {
    x: 410,
    y: 508,
    width: 620,
    height: 320
  })
})

test('defaultHudBounds fits the default layout to a small work area', () => {
  assert.deepEqual(defaultHudBounds({ x: -800, y: 0, width: 480, height: 240 }), {
    x: -800,
    y: 0,
    width: 480,
    height: 240
  })
})

test('defaultHudBounds keeps the spawn fallback when no display is available', () => {
  assert.deepEqual(defaultHudBounds(), { x: undefined, y: undefined, width: 620, height: 320 })
})
