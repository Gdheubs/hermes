import { afterEach, describe, expect, it } from 'vitest'

import {
  clampSize,
  loadViewportMode,
  modeSize,
  parseViewportMode,
  saveViewportMode,
  scaleFor,
  VIEWPORT_LIMITS,
  VIEWPORT_STORAGE_KEY
} from './preview-viewport'

afterEach(() => {
  window.localStorage.clear()
})

describe('clampSize', () => {
  it('keeps values inside the Zcode limits', () => {
    expect(clampSize(1280, 720)).toEqual({ width: 1280, height: 720 })
  })

  it('clamps below the minimum', () => {
    expect(clampSize(10, 10)).toEqual({
      width: VIEWPORT_LIMITS.minWidth,
      height: VIEWPORT_LIMITS.minHeight
    })
  })

  it('clamps above the maximum', () => {
    expect(clampSize(99999, 99999)).toEqual({
      width: VIEWPORT_LIMITS.maxWidth,
      height: VIEWPORT_LIMITS.maxHeight
    })
  })

  it('returns non-finite sizes for NaN so the caller can reject them', () => {
    expect(clampSize(Number('abc'), 720)).toEqual({ width: Number.NaN, height: 720 })
    expect(Number.isFinite(clampSize(Number('abc'), 720).width)).toBe(false)
  })
})

describe('parseViewportMode', () => {
  it('defaults unknown input to free size', () => {
    expect(parseViewportMode(null)).toEqual({ kind: 'free' })
    expect(parseViewportMode({ kind: 'nope' })).toEqual({ kind: 'free' })
  })

  it('accepts presets and custom sizes', () => {
    expect(parseViewportMode({ kind: 'preset', id: 'mobile' })).toEqual({ kind: 'preset', id: 'mobile' })
    expect(parseViewportMode({ kind: 'custom', width: 800, height: 600 })).toEqual({
      kind: 'custom',
      width: 800,
      height: 600
    })
  })
})

describe('modeSize', () => {
  it('returns null for free size', () => {
    expect(modeSize({ kind: 'free' })).toBeNull()
  })

  it('returns preset and custom boxes', () => {
    expect(modeSize({ kind: 'preset', id: 'desktop' })).toEqual({ width: 1920, height: 1080 })
    expect(modeSize({ kind: 'custom', width: 390, height: 844 })).toEqual({ width: 390, height: 844 })
  })
})

describe('scaleFor', () => {
  it('does not upscale a guest that already fits', () => {
    expect(scaleFor({ width: 1920, height: 1080 }, { width: 375, height: 667 })).toBe(1)
  })

  it('scales down to the tighter axis', () => {
    expect(scaleFor({ width: 375, height: 200 }, { width: 750, height: 200 })).toBe(0.5)
  })
})

describe('viewport storage', () => {
  it('round-trips a mode through localStorage', () => {
    saveViewportMode({ kind: 'preset', id: 'laptop' })
    expect(window.localStorage.getItem(VIEWPORT_STORAGE_KEY)).toContain('laptop')
    expect(loadViewportMode()).toEqual({ kind: 'preset', id: 'laptop' })
  })

  it('returns free size when storage is empty or corrupt', () => {
    expect(loadViewportMode()).toEqual({ kind: 'free' })
    window.localStorage.setItem(VIEWPORT_STORAGE_KEY, '{')
    expect(loadViewportMode()).toEqual({ kind: 'free' })
  })
})
