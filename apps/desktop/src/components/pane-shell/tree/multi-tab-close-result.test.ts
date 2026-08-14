import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { registry } from '@/contrib/registry'

import { group } from './model'
import { closeOtherTreeTabs, declareDefaultTree, registerPaneCloser } from './store'

describe('multi-tab close results', () => {
  const disposers: (() => void)[] = []

  beforeEach(() => {
    window.localStorage.clear()
  })

  afterEach(() => {
    disposers.splice(0).forEach(dispose => dispose())

    for (const paneId of ['first', 'center', 'last']) {
      registerPaneCloser(paneId)
    }
  })

  it('does not settle close-others until every deferred closer settles', async () => {
    let resolveFirst: (() => void) | undefined
    let resolveLast: (() => void) | undefined

    for (const paneId of ['first', 'center', 'last']) {
      disposers.push(
        registry.register({ area: 'panes', id: paneId, render: () => null, title: paneId })
      )
    }

    declareDefaultTree(group(['first', 'center', 'last'], { active: 'center', id: 'grp-tabs' }))
    registerPaneCloser(
      'first',
      () =>
        new Promise<void>(resolve => {
          resolveFirst = resolve
        })
    )
    registerPaneCloser(
      'last',
      () =>
        new Promise<void>(resolve => {
          resolveLast = resolve
        })
    )

    const result = closeOtherTreeTabs('center')

    expect(result).toBeInstanceOf(Promise)

    let settled = false
    void Promise.resolve(result).then(() => {
      settled = true
    })
    resolveFirst?.()
    await Promise.resolve()

    expect(settled).toBe(false)

    resolveLast?.()
    await result

    expect(settled).toBe(true)
  })
})
