import { afterEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { host } from '@/sdk'
import { setActiveSessionId, setAwaitingResponse, setBusy } from '@/store/session'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'

describe('host.state turn flags', () => {
  afterEach(() => {
    setActiveSessionId(null)
    setBusy(false)
    setAwaitingResponse(false)
    clearAllSessionStates()
  })

  it('uses the draft atoms when there is no runtime session', () => {
    expect(host.state.busy.get()).toBe(false)
    expect(host.state.awaitingResponse.get()).toBe(false)

    setBusy(true)
    setAwaitingResponse(true)

    expect(host.state.busy.get()).toBe(true)
    expect(host.state.awaitingResponse.get()).toBe(true)
  })

  it('reads the focused session slice once a runtime exists', () => {
    setBusy(false)
    setAwaitingResponse(false)
    setActiveSessionId('rt-focus')
    publishSessionState('rt-focus', {
      ...createClientSessionState('stored-focus'),
      awaitingResponse: true,
      busy: true
    })

    expect(host.state.busy.get()).toBe(true)
    expect(host.state.awaitingResponse.get()).toBe(true)

    publishSessionState('rt-focus', {
      ...createClientSessionState('stored-focus'),
      awaitingResponse: false,
      busy: true
    })

    expect(host.state.busy.get()).toBe(true)
    expect(host.state.awaitingResponse.get()).toBe(false)
  })

  it('does not pick up a background session', () => {
    setActiveSessionId('rt-focus')
    publishSessionState('rt-focus', createClientSessionState('stored-focus'))
    publishSessionState('rt-bg', {
      ...createClientSessionState('stored-bg'),
      awaitingResponse: true,
      busy: true
    })

    expect(host.state.busy.get()).toBe(false)
    expect(host.state.awaitingResponse.get()).toBe(false)
  })

  it('follows a focused session tile, not the primary', async () => {
    const tree = await import('@/components/pane-shell/tree/store')
    const model = await import('@/components/pane-shell/tree/model')
    const { registry } = await import('@/contrib/registry')
    const { $sessionTiles } = await import('@/store/session-states')

    // A second chat zone holding a session tile, next to the main workspace.
    for (const id of ['workspace', 'session-tile:tile-a']) {
      registry.register({
        area: 'panes',
        data: id === 'workspace' ? { placement: 'main', uncloseable: true } : { placement: 'main' },
        id,
        render: () => null,
        title: id
      })
    }

    tree.declareDefaultTree(
      model.split('row', [
        model.group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        model.group(['session-tile:tile-a'], { active: 'session-tile:tile-a', id: 'grp-side' })
      ])
    )

    // Primary chat is idle; the tile's session is mid-turn.
    setActiveSessionId('rt-primary')
    publishSessionState('rt-primary', createClientSessionState('stored-primary'))
    $sessionTiles.set([{ runtimeId: 'rt-tile-a', storedSessionId: 'tile-a' }])
    publishSessionState('rt-tile-a', {
      ...createClientSessionState('tile-a'),
      awaitingResponse: true,
      busy: true
    })

    // Focusing the tile zone moves the flags onto the tile's session…
    tree.noteActiveTreeGroup('grp-side')
    expect(host.state.busy.get()).toBe(true)
    expect(host.state.awaitingResponse.get()).toBe(true)

    // …and homing back to the workspace returns to the (idle) primary.
    tree.noteActiveTreeGroup('grp-main')
    expect(host.state.busy.get()).toBe(false)
    expect(host.state.awaitingResponse.get()).toBe(false)

    $sessionTiles.set([])
  })
})

describe('host.revealPane', () => {
  it('un-dismisses a closed plugin pane and puts it back in the layout tree', async () => {
    const { allPaneIds, group, split } = await import('@/components/pane-shell/tree/model')
    const { $dismissedPanes, $layoutTree, dismissTreePane } = await import('@/components/pane-shell/tree/store')
    const { registry } = await import('@/contrib/registry')

    const disposers = [
      registry.register({
        area: 'panes',
        data: { placement: 'main' },
        id: 'workspace',
        render: () => null,
        title: 'workspace'
      }),
      registry.register({
        area: 'panes',
        data: { placement: 'right' },
        id: 'plugin:reveal-target',
        render: () => null,
        title: 'reveal-target'
      })
    ]

    $layoutTree.set(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['plugin:reveal-target'], { active: 'plugin:reveal-target', id: 'grp-side' })
      ])
    )

    dismissTreePane('plugin:reveal-target')
    expect($dismissedPanes.get()).toContain('plugin:reveal-target')
    expect(allPaneIds($layoutTree.get()!)).not.toContain('plugin:reveal-target')

    host.revealPane('plugin:reveal-target')

    expect($dismissedPanes.get()).not.toContain('plugin:reveal-target')
    expect(allPaneIds($layoutTree.get()!)).toContain('plugin:reveal-target')

    disposers.forEach(dispose => dispose())
    $dismissedPanes.set(new Set())
  })
})
