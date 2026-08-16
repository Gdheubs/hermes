/**
 * Regression ground truth for #84484 — "layout editor discards pane moves" on
 * macOS. A pointer-captured pane drag whose pointer crosses a native
 * `-webkit-app-region: drag` region (frameless macOS windows implement those
 * as OS-level overlays) is stolen from the renderer: the mouseup is eaten and
 * Chromium fires `pointercancel` (or the window blurs / the capture is
 * dropped) instead of `pointerup`. The drag session must then COMMIT the drop
 * at the last resolved position — the user completed a real drag — instead of
 * silently discarding it. Esc stays a hard abort.
 *
 * Drives the REAL `startPaneDrag` machinery against a real DOM (zones as
 * `[data-tree-group]` nodes with live rects), exactly the way the app runs it.
 */

import type { PointerEvent as ReactPointerEvent } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { findGroupOfPane, group, split } from '../model'
import { $layoutTree, $treeDragging } from '../store'

import { startPaneDrag } from './drag-session'

let handle: HTMLElement

const rect = (left: number, right: number) => ({
  bottom: 600,
  height: 600,
  left,
  right,
  toJSON: () => ({}),
  top: 0,
  width: right - left,
  x: left,
  y: 0
})

function makeZone(id: string, left: number, right: number): HTMLElement {
  const el = document.createElement('div')
  el.dataset.treeGroup = id
  el.getBoundingClientRect = () => rect(left, right) as DOMRect

  document.body.append(el)

  return el
}

/** A pointerdown as React's synthetic event would deliver it. */
function press(x: number, y: number): ReactPointerEvent<HTMLElement> {
  return {
    button: 0,
    clientX: x,
    clientY: y,
    currentTarget: handle,
    pointerId: 1,
    preventDefault: () => {},
    shiftKey: false,
    stopPropagation: () => {}
  } as unknown as ReactPointerEvent<HTMLElement>
}

/** The Default-layout shape: workspace on the left, FILES on the right. */
function defaultTree() {
  $layoutTree.set(
    split('row', [
      group(['workspace'], { active: 'workspace', id: 'grp-main' }),
      group(['files'], { active: 'files', id: 'grp-files' })
    ])
  )
}

beforeEach(() => {
  window.localStorage.clear()
  document.body.innerHTML = ''
  handle = document.createElement('div')
  document.body.append(handle)
  $layoutTree.set(null)
  $treeDragging.set(null)
  // rAF-coalesced moves need a frame pump; jsdom has none — fire them inline
  // so every pointermove resolves synchronously and the test is deterministic.
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    cb(performance.now())

    return 1
  })
  vi.stubGlobal('cancelAnimationFrame', () => undefined)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('pane drag gesture end (macOS native drag-region steal)', () => {
  it('pointercancel after the drag engaged COMMITS the drop at the last position', () => {
    makeZone('grp-main', 0, 400)
    makeZone('grp-files', 400, 800)
    defaultTree()

    // Press on the FILES zone, drag across the workspace, then the OS takes
    // the gesture (native drag region) — the release arrives as pointercancel.
    startPaneDrag('files', press(600, 300))
    window.dispatchEvent(new MouseEvent('pointermove', { clientX: 200, clientY: 300 }))

    // Engaged: the zone lit up and the gesture held the native regions off.
    expect($treeDragging.get()).toBe('files')
    expect(document.body.classList.contains('drag-region-lock')).toBe(true)

    window.dispatchEvent(new MouseEvent('pointercancel'))

    // The drop landed — FILES joined the workspace zone instead of staying put.
    expect(findGroupOfPane($layoutTree.get()!, 'files')?.id).toBe('grp-main')
    expect(document.body.classList.contains('drag-region-lock')).toBe(false)
    expect($treeDragging.get()).toBeNull()
  })

  it('lostpointercapture after the drag engaged commits (native overlay took the capture)', () => {
    makeZone('grp-main', 0, 400)
    makeZone('grp-files', 400, 800)
    defaultTree()

    startPaneDrag('files', press(600, 300))
    window.dispatchEvent(new MouseEvent('pointermove', { clientX: 200, clientY: 300 }))

    handle.dispatchEvent(new Event('lostpointercapture'))

    expect(findGroupOfPane($layoutTree.get()!, 'files')?.id).toBe('grp-main')
  })

  it('Esc still aborts an engaged drag — nothing moves', () => {
    makeZone('grp-main', 0, 400)
    makeZone('grp-files', 400, 800)
    defaultTree()

    startPaneDrag('files', press(600, 300))
    window.dispatchEvent(new MouseEvent('pointermove', { clientX: 200, clientY: 300 }))

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))

    expect(findGroupOfPane($layoutTree.get()!, 'files')?.id).toBe('grp-files')
    expect($treeDragging.get()).toBeNull()
  })

  it('a pointercancel before engagement stays a clean abort, not a tap or a move', () => {
    makeZone('grp-main', 0, 400)
    makeZone('grp-files', 400, 800)
    defaultTree()

    startPaneDrag('files', press(600, 300))
    // Sub-threshold (no move beyond 4px): the gesture never engaged.
    window.dispatchEvent(new MouseEvent('pointercancel'))

    expect(findGroupOfPane($layoutTree.get()!, 'files')?.id).toBe('grp-files')
    expect(document.body.classList.contains('drag-region-lock')).toBe(false)
  })

  it('a pointercancel over a deny area commits nothing (hint is null)', () => {
    makeZone('grp-main', 0, 400)
    makeZone('grp-files', 400, 800)
    defaultTree()

    startPaneDrag('files', press(600, 300))
    // Move off every zone — the resolver returns a null hint (deny area).
    window.dispatchEvent(new MouseEvent('pointermove', { clientX: 1000, clientY: 300 }))

    window.dispatchEvent(new MouseEvent('pointercancel'))

    expect(findGroupOfPane($layoutTree.get()!, 'files')?.id).toBe('grp-files')
  })
})
