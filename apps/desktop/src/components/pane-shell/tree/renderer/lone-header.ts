/**
 * When a lone pane must keep its tab strip (name card + close).
 *
 * Default: a single pane isn't a "tab", so the header auto-hides. Exceptions
 * force it on so a closeable surface never becomes an unclosable dead zone:
 *  - a closeable `placement: 'main'` pane — every mirrored TILE (a session, a
 *    page, a preview) is one, so dragging a tile into a zone of its own keeps
 *    its tab and its ✕
 *  - a collapse tool panel dragged into its own zone
 */

export interface LoneHeaderChrome {
  placement?: string
  uncloseable?: boolean
}

export function forceLoneHeaderForPanes(
  shown: readonly string[],
  chromeOf: (id: string) => LoneHeaderChrome,
  isCollapsePane: (id: string) => boolean
): boolean {
  // "This pane can be closed, so it must expose the ✕." Only the uncloseable
  // workspace is exempt; standing side chrome (files / sessions) isn't 'main'.
  if (
    shown.some(id => {
      const chrome = chromeOf(id)

      return !chrome.uncloseable && chrome.placement === 'main'
    })
  ) {
    return true
  }

  return shown.length === 1 && isCollapsePane(shown[0])
}

/** Header visibility for a zone, in precedence order:
 *  1. a full-page view (headerVeto) always suppresses the strip;
 *  2. a LONE closeable tile always keeps its strip — even over a persisted
 *     `headerHidden: true` (a double-tap hide or an older build's leftover).
 *     Without the strip the tile has no tab to grab and no close gesture
 *     reachable over a webview/iframe body: an unclosable dead zone whose
 *     only escape was a layout reset;
 *  3. otherwise the user's persisted choice stands (normalize deliberately
 *     keeps it), defaulting to headerless for lone side chrome. */
export function resolveZoneHeaderHidden(input: {
  headerVeto?: boolean
  persisted?: boolean
  shownCount: number
  forceLoneHeader: boolean
}): boolean {
  if (input.headerVeto) {
    return true
  }

  if (input.shownCount <= 1 && input.forceLoneHeader) {
    return false
  }

  return input.persisted ?? input.shownCount <= 1
}
