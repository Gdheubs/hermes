// HUD mode's window title, plus the guard that keeps it stable.
//
// The pure, Electron-typed piece lives here so it can be unit-tested (same
// split as hud-url.ts). The HUD is chrome-free, so its title is invisible to
// the user — but it is the one stable handle a tiling window manager
// (Hyprland, i3, sway, …) can match on. Without a distinct title the HUD is
// indistinguishable from the main app window, which shares its class/app-id;
// window rules aimed at the HUD would hit both.

/** The HUD window's title — distinct from the main window's "Hermes". */
export const HUD_WINDOW_TITLE = 'Hermes HUD'

/** The minimal window surface this helper needs, kept structural so tests
 *  don't have to construct a real BrowserWindow. */
interface HudTitleWindow {
  setTitle(title: string): void
  on(event: 'page-title-updated', listener: (event: { preventDefault(): void }) => void): void
}

/**
 * Make sure the HUD window's title stays `HUD_WINDOW_TITLE`.
 *
 * The renderer's `<title>Hermes</title>` would otherwise overwrite the window
 * title the moment the page loads, silently destroying the handle window
 * managers match on. The guard is attached before the URL is loaded, so the
 * compositor never sees the generic title.
 */
export function wireHudWindowTitle(win: HudTitleWindow): void {
  win.setTitle(HUD_WINDOW_TITLE)
  win.on('page-title-updated', event => event.preventDefault())
}
