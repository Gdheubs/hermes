import { describe, expect, it } from 'vitest'

import { forceLoneHeaderForPanes, resolveZoneHeaderHidden } from './lone-header'

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

describe('resolveZoneHeaderHidden', () => {
  it('a lone closeable tile keeps its strip even over a persisted hide (dead-zone regression)', () => {
    expect(resolveZoneHeaderHidden({ persisted: true, shownCount: 1, forceLoneHeader: true })).toBe(false)
    expect(resolveZoneHeaderHidden({ persisted: undefined, shownCount: 1, forceLoneHeader: true })).toBe(false)
  })

  it('headerVeto always suppresses the strip', () => {
    expect(resolveZoneHeaderHidden({ headerVeto: true, shownCount: 1, forceLoneHeader: true })).toBe(true)
  })

  it('preserves the persisted choice where no force applies', () => {
    // Lone side chrome the user deliberately hid stays hidden.
    expect(resolveZoneHeaderHidden({ persisted: true, shownCount: 1, forceLoneHeader: false })).toBe(true)
    // A stacked zone (the chat strip) keeps the user's hide too.
    expect(resolveZoneHeaderHidden({ persisted: true, shownCount: 3, forceLoneHeader: true })).toBe(true)
    // An explicitly shown lone zone keeps its bar.
    expect(resolveZoneHeaderHidden({ persisted: false, shownCount: 1, forceLoneHeader: false })).toBe(false)
  })

  it('defaults lone non-tile panes to headerless', () => {
    expect(resolveZoneHeaderHidden({ shownCount: 1, forceLoneHeader: false })).toBe(true)
    expect(resolveZoneHeaderHidden({ shownCount: 2, forceLoneHeader: false })).toBe(false)
  })
})
