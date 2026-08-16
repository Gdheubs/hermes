/**
 * Regression ground truth for #84484 — "pane sizes cannot be adjusted" in the
 * layout editor. The edit-mode veil (z-50) that covers every zone as a pane
 * drag handle was painting ABOVE the resize sashes (z-20), so in the layout
 * editor the divider handles were covered and ungrabbable. The sash must
 * outrank the veil whenever edit mode is on.
 *
 * Renders the REAL `TreeSplit` (and its `TreeGroup` children) in edit mode and
 * asserts the z-index contract between the sash (`role="separator"`) and the
 * veil (the `outline-dashed` cover), in both edit and normal mode.
 */

import { cleanup, render } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'

import { $layoutEditMode } from '../../edit-mode'
import { group, split, type SplitNode } from '../model'
import { $layoutTree } from '../store'

import { TreeSplit } from './tree-split'

class TestResizeObserver {
  disconnect() {}
  observe() {}
  unobserve() {}
}

const disposers: (() => void)[] = []

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', TestResizeObserver)
  // jsdom lacks CSS.escape, which tab-strip-scroll uses in a layout effect.
  vi.stubGlobal('CSS', { ...globalThis.CSS, escape: (value: string) => value })
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
  HTMLElement.prototype.scrollIntoView ??= () => undefined
})

beforeEach(() => {
  window.localStorage.clear()
  $layoutEditMode.set(false)

  for (const [id, data] of [
    ['workspace', { placement: 'main', uncloseable: true }],
    ['files', { placement: 'right' }]
  ] as const) {
    disposers.push(registry.register({ area: 'panes', data, id, render: () => null, title: id }))
  }

  $layoutTree.set(
    split('row', [
      group(['workspace'], { active: 'workspace', id: 'grp-main' }),
      group(['files'], { active: 'files', id: 'grp-files' })
    ])
  )
})

afterEach(() => {
  cleanup()
  disposers.splice(0).forEach(dispose => dispose())
})

const sash = () => document.querySelector<HTMLElement>('[role="separator"]')
const veil = () => document.querySelector<HTMLElement>('[class*="outline-dashed"]')

/** Parse the numeric z-index token (`z-[60]` → 60) from a Tailwind class list. */
const zIndex = (el: HTMLElement | null) => {
  const token = el?.className.split(/\s+/).find(c => /^z-/.test(c)) ?? ''

  return Number.parseInt(token.replace(/[^\d]/g, ''), 10)
}

describe('sash vs edit veil (layout editor)', () => {
  it('the sash outranks the edit veil in edit mode, so dividers stay grabbable', () => {
    $layoutEditMode.set(true)
    render(<TreeSplit node={$layoutTree.get()! as SplitNode} root rootRow />)

    // Both the seam and the veil are mounted; the sash must sit above it.
    expect(sash()).toBeTruthy()
    expect(veil()).toBeTruthy()
    expect(zIndex(sash())).toBeGreaterThan(zIndex(veil()))
  })

  it('keeps the resting z-index outside edit mode', () => {
    $layoutEditMode.set(false)
    render(<TreeSplit node={$layoutTree.get()! as SplitNode} root rootRow />)

    expect(sash()).toBeTruthy()
    // No veil is mounted outside edit mode, and the sash stays at z-20.
    expect(veil()).toBeNull()
    expect(zIndex(sash())).toBe(20)
  })
})
