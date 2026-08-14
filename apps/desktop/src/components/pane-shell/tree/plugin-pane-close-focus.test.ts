import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $pluginDecisions, dropPlugin, publishPlugin } from '@/contrib/plugins-store'
import { registry } from '@/contrib/registry'

import { closeTreePane } from './store'
import { $treeFocusRequest, requestTreeFocusAfterClose, runTreeCloseWithFocusRecovery } from './tree-focus'

describe('plugin pane close focus lifecycle', () => {
  let disposePane: (() => void) | undefined

  beforeEach(() => {
    window.localStorage.clear()
    $pluginDecisions.set({})
    $treeFocusRequest.set(null)
    disposePane = undefined
  })

  afterEach(() => {
    disposePane?.()
    dropPlugin('plugin-close-lifecycle')
    $pluginDecisions.set({})
    $treeFocusRequest.set(null)
    vi.restoreAllMocks()
  })

  it('keeps focus recovery pending until disabling the plugin pane settles', async () => {
    disposePane = registry.register({
      area: 'panes',
      id: 'plugin-pane',
      render: () => null,
      source: 'plugin:plugin-close-lifecycle',
      title: 'Plugin pane'
    })
    const deactivate = vi.fn(() => disposePane?.())

    publishPlugin(
      {
        id: 'plugin-close-lifecycle',
        kind: 'bundled',
        name: 'Plugin close lifecycle',
        status: 'loaded'
      },
      { activate: () => undefined, deactivate }
    )

    const { result } = runTreeCloseWithFocusRecovery('plugin-pane', () => closeTreePane('plugin-pane'))

    expect(deactivate).toHaveBeenCalledOnce()
    expect(result).toBeInstanceOf(Promise)
    expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'plugin-pane', status: 'pending' })

    await result

    expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'plugin-pane', status: 'settled' })
  })

  it('does not replace an outstanding close request with another close', async () => {
    let resolveFirstClose: (() => void) | undefined

    const firstClose = new Promise<void>(resolve => {
      resolveFirstClose = resolve
    })

    const first = runTreeCloseWithFocusRecovery('first-pane', () => firstClose)
    const secondClose = vi.fn()

    const second = runTreeCloseWithFocusRecovery('second-pane', secondClose)

    expect(second.request).toBeNull()
    expect(secondClose).not.toHaveBeenCalled()
    expect($treeFocusRequest.get()).toBe(first.request)

    resolveFirstClose!()
    await first.result
    expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'first-pane', status: 'settled' })
  })

  it('does not let a raw focus request overwrite an outstanding close', () => {
    const first = requestTreeFocusAfterClose('first-pane')
    const second = requestTreeFocusAfterClose('second-pane')

    expect(second).toBe(first)
    expect($treeFocusRequest.get()).toBe(first)
  })
})
