/**
 * When a zone shows chrome it would otherwise go without — the two rules that
 * keep a closeable surface from becoming an unclosable dead zone.
 *
 * `forceLoneHeaderForPanes`: a single pane isn't a "tab", so the header
 * auto-hides. Exceptions force it on:
 *  - a closeable `placement: 'main'` pane — every mirrored TILE (a session, a
 *    page, a preview) is one, so dragging a tile into a zone of its own keeps
 *    its tab and its ✕
 *  - a collapse tool panel dragged into its own zone
 *
 * `showRevealEdge`: the way back from an EXPLICIT hide, which the rule above
 * cannot cover — it decides the default, and an explicit `headerHidden` beats
 * any default. See the predicate for why the edge has to exist.
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

export interface RevealEdgeZone {
  /** No panes — the zone renders its placeholder, not a surface to uncover. */
  isEmpty: boolean
  /** The group's OWN flag: `true` only after a deliberate hide (header
   *  double-tap / the zone menu). `undefined` means "no opinion", and the
   *  contextual default applies — that case needs no edge. */
  headerHidden?: boolean
  /** The ACTIVE pane's `headerVeto` — a full-page view suppressing the strip
   *  on its own, independently of the flag above. */
  headerVetoed?: boolean
  minimized?: boolean
}

/**
 * Whether a zone offers its top-edge reveal strip.
 *
 * Hiding the header takes the tab strip with it, and the strip is the only
 * host of the zone menu — so an explicitly hidden zone had no tab, no ✕ and no
 * menu. For a zone of closeable tiles (a preview, a Browser) that is a surface
 * stranded on screen, recoverable only by ⌘W or a layout reset. The edge is
 * the way back: double-click reveals the header, right-click opens the zone
 * menu.
 *
 * Only an EXPLICIT hide qualifies. A contextual hide (lone side chrome) keeps
 * its clean edge — it is not a state the user chose, and it lifts on its own
 * when the zone gains a tab.
 *
 * `headerVetoed` is the case where BOTH apply: the flag is set AND a full-page
 * view is suppressing the header anyway. Revealing there would be a dead
 * gesture — clearing the flag cannot bring a vetoed header back, and it would
 * take the strip (and with it the zone menu the strip hosts) away on that
 * page. The veto lifts by itself when the page closes, and the flag is still
 * set underneath, so the edge returns then.
 */
export function showRevealEdge({ headerHidden, headerVetoed, isEmpty, minimized }: RevealEdgeZone): boolean {
  // A minimized group IS its header — it still has one, so nothing to reveal.
  return headerHidden === true && !headerVetoed && !minimized && !isEmpty
}
