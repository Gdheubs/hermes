import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'

import { findGroup, group, split } from '../model'
import { $layoutTree, declareDefaultTree } from '../store'

import { TreeGroup } from './tree-group'

// Ground truth for "the header is hidden and there is no way back". The
// predicate is unit-tested in lone-header.test.ts; this covers the WIRING —
// that the strip renders exactly when the predicate says so, and that the
// gestures reaching it actually clear the flag.

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', TestResizeObserver)
  // jsdom lacks CSS.escape, which tab-strip-scroll uses in a layout effect.
  vi.stubGlobal('CSS', { ...globalThis.CSS, escape: (value: string) => value })
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
  HTMLElement.prototype.scrollIntoView ??= () => undefined
})

const disposers: (() => void)[] = []

/** A preview tile: closeable, `placement: 'main'` — the zone class that gets
 *  stranded. `headerVeto` models a full-page view suppressing the header. */
function registerPane(id: string, data: Record<string, unknown> = { placement: 'main' }) {
  disposers.push(registry.register({ area: 'panes', data, id, render: () => null, title: id }))
}

beforeEach(async () => {
  window.localStorage.clear()

  // `declareDefaultTree` only adopts when there is NO tree yet, so without this
  // every case after the first would inherit the previous one's zone — and its
  // `headerHidden`.
  $layoutTree.set(null)

  const { $dismissedPanes, $hiddenTreePanes } = await import('../store')
  $dismissedPanes.set(new Set())
  $hiddenTreePanes.set(new Set())
})

afterEach(() => {
  cleanup()
  disposers.splice(0).forEach(dispose => dispose())
})

/** Build a one-zone-per-pane tree and render the preview zone. */
function renderZone(options: { headerHidden?: boolean; veto?: boolean } = {}) {
  registerPane('workspace', { placement: 'main', uncloseable: true })
  registerPane('preview-tile:file:notes.md', { headerVeto: options.veto, placement: 'main' })

  declareDefaultTree(
    split('row', [
      group(['workspace'], { active: 'workspace', id: 'grp-main' }),
      group(['preview-tile:file:notes.md'], {
        active: 'preview-tile:file:notes.md',
        headerHidden: options.headerHidden,
        id: 'grp-preview'
      })
    ])
  )

  const tree = $layoutTree.get()!
  render(<TreeGroup node={findGroup(tree, 'grp-preview')!} parentAxis="row" />)
}

const edge = () => screen.queryByRole('button', { name: 'Show header' })
const zoneHeaderHidden = () => findGroup($layoutTree.get()!, 'grp-preview')?.headerHidden

describe('the reveal edge', () => {
  it('appears on an explicitly hidden zone, which has no tab strip to close from', () => {
    renderZone({ headerHidden: true })

    expect(edge()).toBeTruthy()
    expect(document.querySelector('[data-tree-tab]')).toBeNull()
  })

  it('stays away while the header is visible', () => {
    renderZone()

    expect(edge()).toBeNull()
    expect(document.querySelector('[data-tree-tab]')).toBeTruthy()
  })

  // The regression this guards: a full-page view suppresses the header on its
  // own, so clearing the flag there reveals nothing and spends the strip —
  // taking the zone menu it hosts with it.
  it('stays away when a full-page view is vetoing the header', () => {
    renderZone({ headerHidden: true, veto: true })

    expect(edge()).toBeNull()
  })

  it('reveals on double-click, not on a stray single click', () => {
    renderZone({ headerHidden: true })

    // detail 1 — a click that was aiming at the content row below.
    fireEvent.click(edge()!, { detail: 1 })
    expect(zoneHeaderHidden()).toBe(true)

    fireEvent.click(edge()!, { detail: 2 })
    expect(zoneHeaderHidden()).toBe(false)
  })

  // Enter / Space on a native button, and anything assistive tech dispatches,
  // arrive as a click with detail 0.
  it('reveals on a keyboard / assistive-tech activation', () => {
    renderZone({ headerHidden: true })

    fireEvent.click(edge()!, { detail: 0 })

    expect(zoneHeaderHidden()).toBe(false)
  })
})
