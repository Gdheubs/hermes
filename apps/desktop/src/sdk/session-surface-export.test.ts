import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SessionSurfaceCore } from '@/app/chat/session-surface'

import { installPluginSdk, sdkImportMap } from './runtime'

import { SessionSurface, type SessionSurfaceProps } from './index'

const sources: string[] = []

beforeEach(() => {
  sources.length = 0
  vi.spyOn(URL, 'createObjectURL').mockImplementation(blob => {
    void (blob as Blob).text().then((source: string) => sources.push(source))

    return `blob:test-${sources.length}`
  })
})

describe('runtime plugin SDK SessionSurface export', () => {
  it('keeps the public component separate from the core lifecycle surface', () => {
    const props: SessionSurfaceProps = { session: { profile: 'work', storedSessionId: 'stored' } }

    expect(props).toEqual({ session: { profile: 'work', storedSessionId: 'stored' } })
    expect(typeof SessionSurfaceCore).toBe('function')
    expect(SessionSurface).not.toBe(SessionSurfaceCore)
  })

  it('installs and automatically includes SessionSurface in the generated SDK shim', async () => {
    installPluginSdk()

    const sdk = (globalThis as typeof globalThis & { __HERMES_PLUGIN_SDK__: Record<string, unknown> })
      .__HERMES_PLUGIN_SDK__

    expect(typeof sdk.SessionSurface).toBe('function')
    expect(sdkImportMap()['@hermes/plugin-sdk']).toMatch(/^blob:test-/)
    await vi.waitFor(() => expect(sources.some(source => source.includes('SessionSurface'))).toBe(true))
  })

  it('never leaks the internal surface internals through the SDK namespace', () => {
    const sdk = (globalThis as typeof globalThis & { __HERMES_PLUGIN_SDK__: Record<string, unknown> })
      .__HERMES_PLUGIN_SDK__

    expect(sdk.SessionSurfaceChat).toBeUndefined()
    expect(sdk.SessionSurfaceCore).toBeUndefined()
    expect(sdk.$sessionStates).toBeUndefined()
  })
})
