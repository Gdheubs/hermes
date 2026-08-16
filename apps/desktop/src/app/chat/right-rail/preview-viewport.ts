/**
 * PREVIEW VIEWPORT — CSS-pixel size of the in-app browser guest.
 *
 * Free-size (default) lets the webview fill the pane. A locked size sets the
 * guest to that CSS box and scales it to fit the host so media queries fire
 * at the chosen width/height. `webview.setSize` is not used — it does not
 * change the CSS viewport.
 *
 * Limits match Zcode `BROWSER_VIEWPORT_LIMITS` (320–3840 × 320–2160).
 * Last mode is stored in localStorage so a second Browser tab can share it.
 */

export const VIEWPORT_STORAGE_KEY = 'hermes.desktop.preview.viewport.v1'

export const VIEWPORT_LIMITS = {
  minWidth: 320,
  maxWidth: 3840,
  minHeight: 320,
  maxHeight: 2160
} as const

export const VIEWPORT_PRESETS = [
  { id: 'desktop', width: 1920, height: 1080 },
  { id: 'laptop', width: 1366, height: 768 },
  { id: 'mobile', width: 375, height: 667 }
] as const

export type ViewportPresetId = (typeof VIEWPORT_PRESETS)[number]['id']

export type ViewportMode =
  { kind: 'free' } | { kind: 'preset'; id: ViewportPresetId } | { kind: 'custom'; width: number; height: number }

export function clampDim(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.round(value)))
}

export function clampSize(width: number, height: number): { width: number; height: number } {
  return {
    width: clampDim(width, VIEWPORT_LIMITS.minWidth, VIEWPORT_LIMITS.maxWidth),
    height: clampDim(height, VIEWPORT_LIMITS.minHeight, VIEWPORT_LIMITS.maxHeight)
  }
}

export function presetSize(id: ViewportPresetId): { width: number; height: number } {
  const preset = VIEWPORT_PRESETS.find(item => item.id === id) ?? VIEWPORT_PRESETS[0]

  return { width: preset.width, height: preset.height }
}

export function modeSize(mode: ViewportMode): { width: number; height: number } | null {
  if (mode.kind === 'free') {
    return null
  }

  if (mode.kind === 'preset') {
    return presetSize(mode.id)
  }

  return clampSize(mode.width, mode.height)
}

export function parseViewportMode(raw: unknown): ViewportMode {
  if (!raw || typeof raw !== 'object') {
    return { kind: 'free' }
  }

  const value = raw as { kind?: unknown; id?: unknown; width?: unknown; height?: unknown }

  if (value.kind === 'preset' && (value.id === 'desktop' || value.id === 'laptop' || value.id === 'mobile')) {
    return { kind: 'preset', id: value.id }
  }

  if (value.kind === 'custom' && typeof value.width === 'number' && typeof value.height === 'number') {
    const size = clampSize(value.width, value.height)

    return { kind: 'custom', width: size.width, height: size.height }
  }

  return { kind: 'free' }
}

export function loadViewportMode(): ViewportMode {
  try {
    const raw = window.localStorage.getItem(VIEWPORT_STORAGE_KEY)

    if (!raw) {
      return { kind: 'free' }
    }

    return parseViewportMode(JSON.parse(raw))
  } catch {
    return { kind: 'free' }
  }
}

export function saveViewportMode(mode: ViewportMode): void {
  try {
    window.localStorage.setItem(VIEWPORT_STORAGE_KEY, JSON.stringify(mode))
  } catch {
    // Nonfatal — private mode / quota.
  }
}

export function scaleFor(host: { width: number; height: number }, guest: { width: number; height: number }): number {
  if (host.width <= 0 || host.height <= 0 || guest.width <= 0 || guest.height <= 0) {
    return 1
  }

  return Math.min(1, host.width / guest.width, host.height / guest.height)
}
