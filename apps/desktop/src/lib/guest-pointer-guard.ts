/**
 * Electron <webview> guests (and iframes) hit-test in their own process, so a
 * pointer-capture drag in the embedder goes silent the moment the cursor
 * crosses one — a sash resize froze after a few pixels of travel into the
 * in-app browser, and only another press could continue it. While a drag is
 * live, make every guest surface transparent to hit-testing (see the
 * `guest-pointer-lock` rule in styles.css) so the window-level pointermove /
 * pointerup listeners keep receiving the gesture.
 */
let depth = 0

/** Suppress pointer events on webview/iframe guests until released. Depth-
 *  counted so overlapping gestures compose; the returned release is
 *  idempotent (drags end through several racing paths — pointerup, blur,
 *  lostpointercapture). */
export function guardGuestPointers(): () => void {
  if (depth === 0) {
    document.body.classList.add('guest-pointer-lock')
  }

  depth += 1
  let released = false

  return () => {
    if (released) {
      return
    }

    released = true
    depth -= 1

    if (depth === 0) {
      document.body.classList.remove('guest-pointer-lock')
    }
  }
}

let regionDepth = 0

/**
 * macOS frameless windows turn every `-webkit-app-region: drag` strip into a
 * NATIVE overlay region the OS hit-tests above the web contents (pointer
 * events over it never reach the renderer, and `pointer-events: none` does
 * NOT disable the region — electron#26114 / #4187). A pointer-captured renderer
 * gesture whose pointer crosses one is handed to the window-drag machinery:
 * the OS eats the mouseup and Chromium fires `pointercancel` instead of
 * `pointerup`. While a renderer drag is live, force every region to no-drag so
 * the gesture's pointer stream is never stolen. Same depth-counted,
 * idempotent-release shape as `guardGuestPointers` (separate counter — the two
 * classes must not share a 0↔1 edge).
 */
export function guardNativeDragRegions(): () => void {
  if (regionDepth === 0) {
    document.body.classList.add('drag-region-lock')
  }

  regionDepth += 1
  let released = false

  return () => {
    if (released) {
      return
    }

    released = true
    regionDepth -= 1

    if (regionDepth === 0) {
      document.body.classList.remove('drag-region-lock')
    }
  }
}
