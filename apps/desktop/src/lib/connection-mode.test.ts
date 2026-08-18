import { describe, expect, it } from 'vitest'

import type { HermesConnection } from '@/global'

import { resolveConnectionMode, withConnectionMode } from './connection-mode'

const conn = (over: Partial<HermesConnection> = {}) =>
  ({ baseUrl: 'http://127.0.0.1:8787', ...over }) as HermesConnection

describe('resolveConnectionMode', () => {
  it.each(['local', 'remote'] as const)('passes through the resolved %s mode', mode => {
    expect(resolveConnectionMode(conn({ mode }))).toBe(mode)
  })

  it.each([
    ['no descriptor yet', null],
    ['bridge unavailable', undefined]
  ])('resolves null when there is %s', (_label, value) => {
    expect(resolveConnectionMode(value)).toBeNull()
  })

  it('resolves null rather than guessing local for an unset mode', () => {
    // An older shell that predates the field. Claiming "local" would tell an
    // extension a gateway-side file is openable here when it may not be.
    expect(resolveConnectionMode(conn())).toBeNull()
    expect(resolveConnectionMode(conn({ mode: 'cloud' as never }))).toBeNull()
  })
})

describe('withConnectionMode', () => {
  it.each(['session.create', 'session.resume', 'prompt.submit'])('stamps the mode onto %s', method => {
    expect(withConnectionMode(method, { text: 'hi' }, 'remote')).toEqual({
      connection_mode: 'remote',
      text: 'hi'
    })
  })

  it('leaves unrelated RPCs untouched', () => {
    const params = { limit: 40 }

    expect(withConnectionMode('session.list', params, 'remote')).toBe(params)
  })

  it('announces an explicit null when the mode is unknown', () => {
    // NOT omission. Omitting the key means "leave the stored value alone" to
    // _remember_connection_mode, so a `local` announced before a reconnect
    // would survive into turns that can no longer prove it. An explicit null
    // normalizes to None and clears it.
    expect(withConnectionMode('prompt.submit', { text: 'hi' }, null)).toEqual({
      connection_mode: null,
      text: 'hi'
    })
  })

  it('overrides a caller-supplied mode with the live one', () => {
    // connection_mode is renderer-owned. The plugin SDK reaches this same
    // door, so a caller value winning would let a plugin on a live REMOTE
    // session announce `local` and be believed.
    expect(withConnectionMode('prompt.submit', { connection_mode: 'local' }, 'remote')).toEqual({
      connection_mode: 'remote'
    })
  })

  it('never lets a caller-supplied local survive an unknown live mode', () => {
    expect(withConnectionMode('prompt.submit', { connection_mode: 'local' }, null)).toEqual({
      connection_mode: null
    })
  })

  it('clears a previously announced local when the live mode goes unknown', () => {
    // The reconnect window: the backend is holding `local` from an earlier
    // turn and this turn cannot resolve a descriptor. Unknown must never be
    // guessed as local, so the announcement has to clear rather than skip.
    const reconnecting = withConnectionMode('prompt.submit', { text: 'hi' }, null)

    expect(reconnecting).toHaveProperty('connection_mode', null)
    expect('connection_mode' in reconnecting).toBe(true)
  })

  it('does not mutate the caller params', () => {
    const params = { text: 'hi' }
    withConnectionMode('prompt.submit', params, 'local')

    expect(params).toEqual({ text: 'hi' })
  })
})
