import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const gatewayMocks = vi.hoisted(() => ({
  instances: [] as Array<{
    close: ReturnType<typeof vi.fn>
    connect: ReturnType<typeof vi.fn>
    connectionState: string
    onEvent: ReturnType<typeof vi.fn>
    onState: ReturnType<typeof vi.fn>
  }>,
  setConnection: vi.fn(),
  setGatewayState: vi.fn()
}))

vi.mock('@/hermes', () => ({
  HermesGateway: class {
    connectionState = 'closed'
    close = vi.fn(() => {
      this.connectionState = 'closed'
    })
    connect = vi.fn(async () => {
      this.connectionState = 'open'
    })
    onEvent = vi.fn(() => () => {})
    onState = vi.fn(() => () => {})

    constructor() {
      gatewayMocks.instances.push(this)
    }
  }
}))
vi.mock('@/store/session', () => ({
  setConnection: gatewayMocks.setConnection,
  setGatewayState: gatewayMocks.setGatewayState
}))
vi.mock('@/store/notify-baseline', () => ({ markNativeNotifyBaseline: vi.fn() }))

const {
  $gateway,
  closeSecondaryGateways,
  configureGatewayRegistry,
  ensureGatewayForAgent,
  gatewayForScope,
  gatewaySourceScopeFromEvent,
  setPrimaryGateway
} = await import('./gateway')

beforeEach(() => {
  gatewayMocks.instances.length = 0
  configureGatewayRegistry({ onEvent: vi.fn() })
})

afterEach(() => {
  closeSecondaryGateways()
  setPrimaryGateway(null)
  $gateway.set(null)
  vi.clearAllMocks()
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('gateway source routing', () => {
  it('normalizes event scopes and rejects malformed source identifiers', () => {
    expect(gatewaySourceScopeFromEvent({ connectionId: ' remote-a ', profile: ' worker ' })).toEqual({
      connectionId: 'remote-a',
      profile: 'worker'
    })
    expect(gatewaySourceScopeFromEvent({ profile: 'worker' })).toEqual({ connectionId: null, profile: 'worker' })
    expect(gatewaySourceScopeFromEvent({ connectionId: '', profile: 'worker' })).toBeNull()
    expect(gatewaySourceScopeFromEvent({ connectionId: 'remote-a', profile: '   ' })).toBeNull()
  })

  it('returns only the exact primary source and never falls back to the active gateway', () => {
    const source = { connectionState: 'open', request: vi.fn() }
    const active = { connectionState: 'open', request: vi.fn() }

    setPrimaryGateway(source as never, 'worker', 'source-a')
    $gateway.set(active as never)

    expect(gatewayForScope({ connectionId: 'source-a', profile: 'worker' })).toBe(source)
    expect(gatewayForScope({ connectionId: 'source-b', profile: 'worker' })).toBeNull()
    expect(gatewayForScope(null)).toBeNull()
  })

  it('resolves a registered secondary only by its connection and profile pair', async () => {
    const primary = { connectionState: 'open' }
    setPrimaryGateway(primary as never, 'default', 'primary')
    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
      getConnectionFor: vi.fn(async () => ({ wsUrl: 'ws://source-b.invalid/api/ws' })),
      touchBackend: vi.fn(async () => undefined)
    }

    await ensureGatewayForAgent('source-b', 'worker')

    const secondary = gatewayMocks.instances[0]
    expect(secondary).toBeDefined()
    expect(gatewayForScope({ connectionId: 'source-b', profile: 'worker' })).toBe(secondary)
    expect(gatewayForScope({ connectionId: 'source-b', profile: 'default' })).toBeNull()
  })
})
