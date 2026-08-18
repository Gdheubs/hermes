import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'
import { createClientSessionState } from '@/lib/chat-runtime'
import { host } from '@/sdk'
import { $gateway } from '@/store/gateway'
import { setActiveSessionId, setAwaitingResponse, setBusy, setConnection } from '@/store/session'
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

/**
 * The plugin SDK's `host.request` door must announce the Desktop connection
 * mode on session/prompt RPCs exactly like `useGatewayRequest` does — it used
 * to send straight through `$gateway.get().request()`, letting a runtime
 * plugin create or drive a session whose skills/MCP context never learned the
 * mode (#82187 follow-up review, item 3).
 */

const conn = (mode?: 'local' | 'remote') => ({ baseUrl: 'http://127.0.0.1:8787', mode }) as unknown as HermesConnection

describe('host.request connection-mode announcement', () => {
  afterEach(() => {
    $gateway.set(null as never)
    setConnection(null)
  })

  const installGateway = () => {
    const request = vi.fn().mockResolvedValue('ok')
    $gateway.set({ request } as never)

    return request
  }

  it.each(['session.create', 'session.resume', 'prompt.submit'])('stamps the live mode onto %s', async method => {
    setConnection(conn('remote'))
    const request = installGateway()

    await expect(host.request(method, { text: 'hi' })).resolves.toBe('ok')
    expect(request).toHaveBeenCalledWith(method, { connection_mode: 'remote', text: 'hi' })
  })

  it('leaves unrelated RPCs untouched', async () => {
    setConnection(conn('remote'))
    const request = installGateway()
    const params = { limit: 3 }

    await host.request('session.list', params)

    expect(request).toHaveBeenCalledWith('session.list', params)
  })

  it('announces an explicit null when the mode is unknown', async () => {
    // Null descriptor (reconnect window / older shell). Announcing an explicit
    // null CLEARS the backend's remembered mode; omitting the key leaves it
    // alone (`_remember_connection_mode` only writes when the key is present),
    // so a `local` announced before the reconnect would survive into turns
    // that can no longer prove it. Unknown must never read as local.
    const request = installGateway()

    await host.request('prompt.submit', { text: 'hi' })

    expect(request).toHaveBeenCalledWith('prompt.submit', { connection_mode: null, text: 'hi' })
  })

  it('still throws when no gateway socket is live', async () => {
    setConnection(conn('remote'))

    await expect(host.request('prompt.submit', {})).rejects.toThrow('Hermes gateway unavailable')
  })
})

/**
 * `host.getGateway()` is the SDK's OTHER request door. It hands out the live
 * instance for components that take a `HermesGateway` prop, so a plugin can
 * reach `getGateway().request(...)` — which would otherwise bypass the
 * announcement that `host.request` performs and reopen exactly the gap item 3
 * of the follow-up review closed.
 */
describe('host.getGateway connection-mode announcement', () => {
  afterEach(() => {
    $gateway.set(null as never)
    setConnection(null)
  })

  it('announces on session/prompt RPCs through the returned instance', async () => {
    setConnection(conn('remote'))
    const request = vi.fn().mockResolvedValue('ok')
    $gateway.set({ request } as never)

    await host.getGateway()?.request('prompt.submit', { text: 'hi' })

    expect(request).toHaveBeenCalledWith('prompt.submit', { connection_mode: 'remote', text: 'hi' })
  })

  it('leaves unrelated RPCs untouched', async () => {
    setConnection(conn('remote'))
    const request = vi.fn().mockResolvedValue('ok')
    $gateway.set({ request } as never)
    const params = { limit: 3 }

    await host.getGateway()?.request('session.list', params)

    expect(request).toHaveBeenCalledWith('session.list', params)
  })

  it('forwards the timeout and abort signal the caller passed', async () => {
    // HermesGateway.request is (method, params, timeoutMs, signal). The
    // announcing wrapper stands in for it, so a two-argument wrapper silently
    // dropped arguments three and four: the request fell back to the default
    // deadline and could no longer be aborted. Every other test here calls the
    // two-argument form, so green CI did not cover it.
    setConnection(conn('remote'))
    const request = vi.fn().mockResolvedValue('ok')
    $gateway.set({ request } as never)
    const controller = new AbortController()

    await host.getGateway()?.request('prompt.submit', { text: 'hi' }, 1234, controller.signal)

    expect(request).toHaveBeenCalledWith(
      'prompt.submit',
      { connection_mode: 'remote', text: 'hi' },
      1234,
      controller.signal
    )
    // Identity, not shape: a copied-but-equal signal would abort nothing.
    expect(request.mock.calls[0][3]).toBe(controller.signal)
  })

  it('forwards a timeout and signal on RPCs it does not stamp', async () => {
    setConnection(conn('remote'))
    const request = vi.fn().mockResolvedValue('ok')
    $gateway.set({ request } as never)
    const controller = new AbortController()
    const params = { limit: 3 }

    await host.getGateway()?.request('session.list', params, 99, controller.signal)

    expect(request).toHaveBeenCalledWith('session.list', params, 99, controller.signal)
  })

  it('delegates non-request members to the real instance', () => {
    const close = vi.fn()
    setConnection(conn('remote'))
    $gateway.set({ close, request: vi.fn(), wsUrl: 'ws://127.0.0.1:8787' } as never)

    const gateway = host.getGateway() as unknown as { close: () => void; wsUrl: string }

    expect(gateway.wsUrl).toBe('ws://127.0.0.1:8787')
    gateway.close()
    expect(close).toHaveBeenCalledOnce()
  })

  it('hands back a stable reference for one live gateway', () => {
    // SDK components take this as a React prop; a fresh wrapper per call would
    // churn every memo/effect dependency keyed on it.
    setConnection(conn('remote'))
    $gateway.set({ request: vi.fn() } as never)

    expect(host.getGateway()).toBe(host.getGateway())
  })

  it('stays null before the first socket opens', () => {
    expect(host.getGateway()).toBeNull()
  })
})
