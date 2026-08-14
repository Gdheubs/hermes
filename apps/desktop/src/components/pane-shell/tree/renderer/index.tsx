/**
 * Layout tree renderer (root).
 *
 * - `split` -> flex row/column; 1px seams between siblings double as resize
 *   sashes (the seam IS the boundary — junction-owned, never doubled). See
 *   tree-split.tsx.
 * - `group` -> a ZONE: header strip (tabs when stacked, minimize chevron) +
 *   the active pane's content, resolved from the contribution registry
 *   (`area: 'panes'`). Empty zones exist only in editor-authored trees. See
 *   tree-group.tsx.
 *
 * Dragging is FancyZones-style (drag-session.ts): the LAYOUT STAYS FIXED and
 * every zone lights up as a whole-region drop target; dropping moves the pane
 * into that zone (joining its tab stack). Structure changes (splitting/merging/
 * resizing zones) belong to the zone editor, not the drag.
 *
 * This file owns only the composition: the recursive tree, the narrow-viewport
 * overlays, the edit palette, and the zone editor. The pieces live in sibling
 * modules — track-model (sizing), drag-session (drag), tree-split / tree-group
 * (nodes), layout-picker + edit-bar (edit mode), narrow-overlays.
 */

import { useStore } from '@nanostores/react'
import { type ReactNode, useEffect } from 'react'

import { useLayoutEditHotkey } from '../../edit-mode'
import { publishWorkspaceGeometry } from '../../geometry'
import { findGroup, type LayoutNode } from '../model'
import { $collapsedTreeSides, $hiddenTreePanes, $layoutTree, trackActiveTreeGroup } from '../store'
import { $treeFocusRequest, clearTreeFocusRequest } from '../tree-focus'
import { ZoneEditor } from '../zone-editor'

import { TreeEditBar } from './edit-bar'
import { FloatingPanes } from './floating-panes'
import { NarrowOverlays } from './narrow-overlays'
import { TreeNode } from './tree-node'

function isVisible(element: HTMLElement): boolean {
  for (let current: HTMLElement | null = element; current; current = current.parentElement) {
    const style = window.getComputedStyle(current)

    if (current.hidden || current.getAttribute('aria-hidden') === 'true' || style.display === 'none' || style.visibility === 'hidden') {
      return false
    }
  }

  return true
}

function closeTabControlIsVisible(paneId: string): boolean {
  const tab = Array.from(document.querySelectorAll<HTMLElement>('[data-tree-tab]')).find(
    element => element.dataset.treeTab === paneId
  )

  const closeControl = tab?.querySelector<HTMLElement>('[data-pane-tab-close="true"]')

  return Boolean(closeControl && isVisible(closeControl))
}

function activeElementNeedsRecovery(): boolean {
  const active = document.activeElement

  return !(active instanceof HTMLElement) || active === document.body || !isVisible(active)
}

function focusTabControl(paneId: string): boolean {
  const tab = Array.from(document.querySelectorAll<HTMLElement>('[data-tree-tab]')).find(
    element => element.dataset.treeTab === paneId
  )

  const control = tab?.querySelector<HTMLElement>('[data-pane-tab-control="true"]')

  if (!control || !isVisible(control)) {
    return false
  }

  control.focus({ preventScroll: true })

  return document.activeElement === control
}

function selectedTabControl(root: ParentNode = document): HTMLElement | undefined {
  return Array.from(root.querySelectorAll<HTMLElement>('[data-pane-tab-control="true"]')).find(
    control => control.getAttribute('aria-selected') === 'true' && isVisible(control)
  )
}

function focusSelectedTabInGroup(groupId: string): boolean {
  const group = Array.from(document.querySelectorAll<HTMLElement>('[data-tree-group]')).find(
    element => element.dataset.treeGroup === groupId
  )

  const selectedTab = group && selectedTabControl(group)

  if (!selectedTab) {
    return false
  }

  selectedTab.focus({ preventScroll: true })

  return document.activeElement === selectedTab
}

function focusApplicationFallback(tree: LayoutNode | null, preferredGroupId?: string): void {
  const preferredPaneId = tree && preferredGroupId ? findGroup(tree, preferredGroupId)?.active : null

  if (preferredPaneId && focusTabControl(preferredPaneId)) {
    return
  }

  if (preferredGroupId && focusSelectedTabInGroup(preferredGroupId)) {
    return
  }

  const selectedTab = selectedTabControl()

  if (selectedTab) {
    selectedTab.focus({ preventScroll: true })

    return
  }

  const findVisibleFocusable = (selector: string) =>
    Array.from(document.querySelectorAll<HTMLElement>(selector)).find(isVisible)

  // Prefer an editor over nearby toolbar buttons: after a focused close, the
  // composer is the useful application-level continuation point.
  const focusable =
    findVisibleFocusable(
      '[data-tree-group] [contenteditable="true"], [data-tree-group] textarea:not([disabled]), [data-tree-group] input:not([disabled]):not([type="hidden"])'
    ) ?? findVisibleFocusable('[data-tree-group] button:not([disabled]), [data-tree-group] [href]')

  focusable?.focus({ preventScroll: true })
}

export function LayoutTreeRoot({ children }: { children?: ReactNode }) {
  const tree = useStore($layoutTree)
  const collapsedTreeSides = useStore($collapsedTreeSides)
  const hiddenTreePanes = useStore($hiddenTreePanes)
  const focusRequest = useStore($treeFocusRequest)

  useLayoutEditHotkey(true)

  useEffect(
    () => () => {
      $treeFocusRequest.set(null)
    },
    []
  )

  useEffect(trackActiveTreeGroup, [])
  // Publish --workspace-left/right so chrome (titlebar title) aligns to the
  // main pane's geometry in plain CSS.
  useEffect(publishWorkspaceGeometry, [])
  useEffect(() => {
    if (!focusRequest) {
      return
    }

    if (focusRequest.kind === 'restore') {
      const group = tree ? findGroup(tree, focusRequest.groupId) : null

      if (group?.minimized) {
        return
      }

      const frame = window.requestAnimationFrame(() => {
        if (activeElementNeedsRecovery() && !focusTabControl(focusRequest.paneId)) {
          focusApplicationFallback(tree)
        }

        clearTreeFocusRequest(focusRequest)
      })

      return () => window.cancelAnimationFrame(frame)
    }

    // A busy session can remove its tab before its confirmation dialog closes.
    // Leave focus inside that modal until its closer explicitly settles.
    if (focusRequest.status === 'pending') {
      return
    }

    const frame = window.requestAnimationFrame(() => {
      const sourceControlIsVisible = closeTabControlIsVisible(focusRequest.closedPaneId)

      if (activeElementNeedsRecovery() && (!sourceControlIsVisible || !focusTabControl(focusRequest.closedPaneId))) {
        focusApplicationFallback(tree, focusRequest.groupId)
      }

      clearTreeFocusRequest(focusRequest)
    })

    return () => window.cancelAnimationFrame(frame)
  }, [collapsedTreeSides, focusRequest, hiddenTreePanes, tree])

  if (!tree) {
    return null
  }

  return (
    <div className="relative flex min-h-0 min-w-0 flex-1">
      {/* ZonesOverlay::GetAnimationAlpha ramp: clamp(t / 200ms, 0.001, 1). */}
      <style>{`@keyframes hermes-zone-fade { from { opacity: 0.001 } to { opacity: 1 } }`}</style>
      {/* THE SEAM INVARIANT: boundaries are drawn by the tree (one sash
          hairline per seam) — content mounted in a zone must not paint its
          own edge chrome. App components (asides, the shadcn sidebar) carry
          edge borders + inset highlights for the OLD shell's geometry; this
          neutralizes all of them at the zone boundary, for every current and
          future pane, instead of per-pane class surgery. */}
      <style>{`
        [data-tree-group] :is(aside, [data-slot=sidebar]) {
          border-left-width: 0;
          border-right-width: 0;
          box-shadow: none;
        }
        /* Old-shell titlebar BANDS (chat's session header et al size to
           --titlebar-height, which is 0 inside zones): a zero-height band is
           non-functional but still paints its border-b — a stray hairline
           doubling the zone's top seam. Remove the band entirely. */
        [data-tree-group] header[class*="h-(--titlebar-height)"] {
          display: none;
        }
      `}</style>
      <TreeNode node={tree} root rootRow={tree.type === 'split' && tree.orientation === 'row'} />
      <NarrowOverlays />
      {/* Non-tiling panes: fixed cards above the tree, outside every zone. */}
      <FloatingPanes />
      <TreeEditBar />
      <ZoneEditor />
      {children}
    </div>
  )
}
