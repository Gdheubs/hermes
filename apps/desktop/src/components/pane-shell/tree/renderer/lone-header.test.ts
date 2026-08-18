import { describe, expect, it } from 'vitest'

import { forceLoneHeaderForPanes, showRevealEdge } from './lone-header'

describe('forceLoneHeaderForPanes', () => {
  const chrome =
    (placement?: string, uncloseable = false) =>
    () => ({ placement, uncloseable })

  const noCollapse = () => false

  // Every mirrored tile (session / page / preview) is a closeable `main` pane, so
  // dragging one into a zone of its own must keep its tab — it used to strand a
  // preview headerless, with nothing to grab and no ✕.
  it('forces a header for closeable placement:main panes', () => {
    expect(forceLoneHeaderForPanes(['preview-tile:url:x'], chrome('main'), noCollapse)).toBe(true)
    expect(forceLoneHeaderForPanes(['session-tile:abc'], chrome('main'), noCollapse)).toBe(true)
  })

  it('forces a header for a lone collapse tool pane', () => {
    expect(
      forceLoneHeaderForPanes(
        ['terminal'],
        () => ({}),
        id => id === 'terminal'
      )
    ).toBe(true)
  })

  it('leaves a lone uncloseable workspace headerless', () => {
    expect(forceLoneHeaderForPanes(['workspace'], chrome('main', true), noCollapse)).toBe(false)
  })

  it('leaves standing side chrome (files / sessions) headerless', () => {
    expect(forceLoneHeaderForPanes(['files'], chrome('right'), noCollapse)).toBe(false)
  })
})

describe('showRevealEdge', () => {
  // The trap this exists for: hiding the header takes the tab strip — and with
  // it the only host of the zone menu — so a preview / Browser zone was left
  // with no tab, no ✕ and no menu, closeable only by ⌘W or a layout reset.
  it('offers the edge to a zone whose header was explicitly hidden', () => {
    expect(showRevealEdge({ headerHidden: true, isEmpty: false })).toBe(true)
  })

  // A contextual hide is not a state the user chose, and it lifts on its own
  // (the zone gains a tab, the page closes) — no edge, no 6px of chrome.
  it('leaves a contextually headerless zone alone', () => {
    expect(showRevealEdge({ isEmpty: false })).toBe(false)
    expect(showRevealEdge({ headerHidden: false, isEmpty: false })).toBe(false)
  })

  it('skips a minimized group — it IS its header, so nothing is hidden', () => {
    expect(showRevealEdge({ headerHidden: true, isEmpty: false, minimized: true })).toBe(false)
  })

  it('skips an empty zone — its placeholder is not a covered surface', () => {
    expect(showRevealEdge({ headerHidden: true, isEmpty: true })).toBe(false)
  })

  // Revealing under a full-page veto is a dead gesture: clearing the flag
  // cannot bring a vetoed header back, and it would take the strip — the zone
  // menu's only host on that page — away for nothing.
  it('skips a zone whose header is vetoed by a full-page view', () => {
    expect(showRevealEdge({ headerHidden: true, headerVetoed: true, isEmpty: false })).toBe(false)
  })

  // The veto lifts on its own when the page closes, and the flag is still set
  // underneath — so the edge comes back rather than staying spent.
  it('offers the edge again once the veto lifts', () => {
    expect(showRevealEdge({ headerHidden: true, headerVetoed: false, isEmpty: false })).toBe(true)
  })
})
