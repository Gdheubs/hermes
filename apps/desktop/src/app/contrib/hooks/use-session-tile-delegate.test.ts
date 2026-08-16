import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesModule from '@/hermes'
import { createClientSessionState } from '@/lib/chat-runtime'
import { setSessions } from '@/store/session'
import { sessionRuntimeStateKey, sessionTileDelegate } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { useSessionTileDelegate } from './use-session-tile-delegate'

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<typeof HermesModule>()),
  getLatestSessionMessages: vi.fn(async () => ({ messages: [], session_id: '' }))
}))
vi.mock('@/store/gateway', () => ({
  requestGatewayForProfile: vi.fn()
}))

const { getLatestSessionMessages } = await import('@/hermes')
const { requestGatewayForProfile } = await import('@/store/gateway')

const row = (over: Partial<SessionInfo>): SessionInfo =>
  ({
    ended_at: null,
    id: 'live',
    input_tokens: 0,
    is_active: false,
    last_active: 0,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    profile: 'default',
    source: null,
    started_at: 0,
    title: null,
    ...over
  }) as SessionInfo

function renderTile(
  requestGateway: ReturnType<typeof vi.fn>,
  refs?: {
    runtimeIdByStoredSessionIdRef?: { current: Map<string, string> }
    sessionStateByRuntimeIdRef?: { current: Map<string, unknown> }
    updateSessionState?: ReturnType<typeof vi.fn>
  }
) {
  renderHook(() =>
    useSessionTileDelegate({
      archiveSession: vi.fn(async () => undefined),
      branchStoredSession: vi.fn(async () => undefined),
      executeSlashCommand: vi.fn(async () => undefined) as never,
      removeSession: vi.fn(async () => undefined),
      requestGateway: requestGateway as never,
      runtimeIdByStoredSessionIdRef: (refs?.runtimeIdByStoredSessionIdRef ?? { current: new Map() }) as never,
      sessionStateByRuntimeIdRef: (refs?.sessionStateByRuntimeIdRef ?? { current: new Map() }) as never,
      updateSessionState: (refs?.updateSessionState ?? vi.fn()) as never
    })
  )
}

describe('useSessionTileDelegate resumeTile', () => {
  beforeEach(() => {
    setSessions([])
    vi.mocked(getLatestSessionMessages).mockClear()
  })

  afterEach(() => {
    setSessions([])
  })

  it('carries the owning profile into a cold tile resume so it cannot fork profiles', async () => {
    // A tile opens a session owned by another profile. Resuming without the
    // profile lets the gateway fall back to the launch-profile DB and clone the
    // conversation into the wrong profile (#67603). The owning profile must ride
    // both the transcript prefetch and the resume RPC.
    setSessions([row({ id: 'stored-x', profile: 'ai-engineer' })])

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-1' } as never) : ({} as never)
    )

    renderTile(requestGateway)
    const runtimeId = await sessionTileDelegate()!.resumeTile('stored-x')

    expect(runtimeId).toBe('runtime-1')
    expect(getLatestSessionMessages).toHaveBeenCalledWith('stored-x', 'ai-engineer')
    expect(requestGateway).toHaveBeenCalledWith('session.resume', {
      session_id: 'stored-x',
      cols: 96,
      profile: 'ai-engineer',
      omit_messages: true
    })
  })

  it('resolves and carries a default-profile session explicitly', async () => {
    setSessions([row({ id: 'stored-y', profile: 'default' })])

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-2' } as never) : ({} as never)
    )

    renderTile(requestGateway)
    await sessionTileDelegate()!.resumeTile('stored-y')

    expect(requestGateway).toHaveBeenCalledWith('session.resume', {
      session_id: 'stored-y',
      cols: 96,
      profile: 'default',
      omit_messages: true
    })
  })

  it('reuses a warm binding that still carries a transcript', async () => {
    const stateA = { busy: false, messages: [{ id: 'm1' }], storedSessionId: 'stored-a' }
    const runtimeIdByStoredSessionIdRef = { current: new Map([['stored-a', 'runtime-a']]) }
    const sessionStateByRuntimeIdRef = { current: new Map([['runtime-a', stateA]]) }
    const requestGateway = vi.fn(async () => ({}) as never)

    renderTile(requestGateway, { runtimeIdByStoredSessionIdRef, sessionStateByRuntimeIdRef })
    const runtimeId = await sessionTileDelegate()!.resumeTile('stored-a')

    expect(runtimeId).toBe('runtime-a')
    expect(requestGateway).not.toHaveBeenCalled()
  })

  it('falls through to a real resume when the warm binding has no transcript (post-wake empty tile)', async () => {
    // Sleep/wake regression: a released/stale cached state (messages: []) must
    // NOT satisfy the warm path — reusing it re-bound the tile to a dead
    // runtime id and painted the pane permanently empty.
    setSessions([row({ id: 'stored-b', profile: 'default' })])

    const staleState = { busy: false, messages: [], storedSessionId: 'stored-b' }
    const runtimeIdByStoredSessionIdRef = { current: new Map([['stored-b', 'runtime-dead']]) }
    const sessionStateByRuntimeIdRef = { current: new Map([['runtime-dead', staleState]]) }

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-fresh' } as never) : ({} as never)
    )

    renderTile(requestGateway, { runtimeIdByStoredSessionIdRef, sessionStateByRuntimeIdRef })
    const runtimeId = await sessionTileDelegate()!.resumeTile('stored-b')

    expect(runtimeId).toBe('runtime-fresh')
    expect(requestGateway).toHaveBeenCalledWith('session.resume', {
      session_id: 'stored-b',
      cols: 96,
      profile: 'default',
      omit_messages: true
    })
  })

  it('invalidateRuntimeBindings clears the stored→runtime map so tiles re-resume after reconnect', async () => {
    setSessions([row({ id: 'stored-c', profile: 'default' })])

    const liveState = { busy: false, messages: [{ id: 'm1' }], storedSessionId: 'stored-c' }
    const runtimeIdByStoredSessionIdRef = { current: new Map([['stored-c', 'runtime-dead']]) }
    const sessionStateByRuntimeIdRef = { current: new Map([['runtime-dead', liveState]]) }

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-fresh' } as never) : ({} as never)
    )

    renderTile(requestGateway, { runtimeIdByStoredSessionIdRef, sessionStateByRuntimeIdRef })

    // Gateway reconnect (what resetTileRuntimeBindings calls on wake):
    sessionTileDelegate()!.invalidateRuntimeBindings!()
    expect(runtimeIdByStoredSessionIdRef.current.size).toBe(0)

    // The next resume goes cold instead of reusing the dead binding.
    const runtimeId = await sessionTileDelegate()!.resumeTile('stored-c')
    expect(runtimeId).toBe('runtime-fresh')
  })
})

describe('useSessionTileDelegate SessionSurface isolation', () => {
  beforeEach(() => {
    vi.mocked(requestGatewayForProfile).mockReset()
  })

  it('validates a runtime hint on its owner socket before adopting it', async () => {
    vi.mocked(requestGatewayForProfile).mockResolvedValue({ output: 'Hermes TUI Status\n\nSession ID: stored-safe' })
    const requestGateway = vi.fn()
    renderTile(requestGateway)

    await expect(
      sessionTileDelegate()!.adoptSurface({
        profile: 'work',
        runtimeSessionId: 'runtime-safe',
        storedSessionId: 'stored-safe'
      })
    ).resolves.toBe('runtime-safe')

    expect(requestGatewayForProfile).toHaveBeenCalledWith('work', 'session.status', {
      session_id: 'runtime-safe'
    })
    expect(requestGateway).not.toHaveBeenCalled()
  })

  it('rejects a stale or cross-profile runtime hint before exposing it', async () => {
    vi.mocked(requestGatewayForProfile).mockResolvedValue({ output: 'Hermes TUI Status\n\nSession ID: stored-other' })
    renderTile(vi.fn())

    await expect(
      sessionTileDelegate()!.adoptSurface({
        profile: 'work',
        runtimeSessionId: 'runtime-collision',
        storedSessionId: 'stored-safe'
      })
    ).rejects.toThrow('Session surface identity mismatch')
  })

  it('classifies a missing hinted runtime as stale for bounded durable recovery', async () => {
    vi.mocked(requestGatewayForProfile).mockRejectedValue(new Error('4007 Session not found'))
    renderTile(vi.fn())

    await expect(
      sessionTileDelegate()!.adoptSurface({
        profile: 'work',
        runtimeSessionId: 'runtime-gone',
        storedSessionId: 'stored-safe'
      })
    ).rejects.toMatchObject({ name: 'StaleSessionSurfaceRuntimeError' })
  })

  it('drops a previously adopted cache entry before durable recovery', async () => {
    const sessionStateByRuntimeIdRef = { current: new Map<string, ReturnType<typeof createClientSessionState>>() }
    let statusAttempts = 0

    vi.mocked(requestGatewayForProfile).mockImplementation(async (_profile, method) => {
      if (method === 'session.status') {
        statusAttempts += 1

        if (statusAttempts === 1) {
          return { output: 'Hermes TUI Status\n\nSession ID: stored-work' } as never
        }

        throw new Error('4007 Session not found')
      }

      if (method === 'session.resume') {
        return { session_id: 'runtime-fresh' } as never
      }

      return {} as never
    })

    renderHook(() =>
      useSessionTileDelegate({
        archiveSession: vi.fn(async () => undefined),
        branchStoredSession: vi.fn(async () => undefined),
        executeSlashCommand: vi.fn(async () => undefined) as never,
        removeSession: vi.fn(async () => undefined),
        requestGateway: vi.fn() as never,
        runtimeIdByStoredSessionIdRef: { current: new Map() },
        sessionStateByRuntimeIdRef: sessionStateByRuntimeIdRef as never,
        updateSessionState: ((runtimeId: string, updater: (state: ReturnType<typeof createClientSessionState>) => ReturnType<typeof createClientSessionState>, storedSessionId?: string, profile?: string) => {
          const key = sessionRuntimeStateKey(profile, runtimeId)
          const next = updater(sessionStateByRuntimeIdRef.current.get(key) ?? createClientSessionState())
          sessionStateByRuntimeIdRef.current.set(key, { ...next, storedSessionId: storedSessionId ?? null })

          return next
        }) as never
      })
    )

    const identity = { profile: 'work', runtimeSessionId: 'runtime-old', storedSessionId: 'stored-work' }
    await expect(sessionTileDelegate()!.adoptSurface(identity)).resolves.toBe('runtime-old')
    await expect(sessionTileDelegate()!.adoptSurface(identity)).rejects.toMatchObject({
      name: 'StaleSessionSurfaceRuntimeError'
    })

    await expect(sessionTileDelegate()!.resumeSurface(identity)).resolves.toBe('runtime-fresh')
    expect(requestGatewayForProfile).toHaveBeenCalledWith('work', 'session.resume', expect.any(Object))
  })

  it('does not classify an authorization failure as a stale hinted runtime', async () => {
    vi.mocked(requestGatewayForProfile).mockRejectedValue(new Error('403 forbidden'))
    renderTile(vi.fn())

    await expect(
      sessionTileDelegate()!.adoptSurface({
        profile: 'work',
        runtimeSessionId: 'runtime-private',
        storedSessionId: 'stored-safe'
      })
    ).rejects.not.toMatchObject({ name: 'StaleSessionSurfaceRuntimeError' })
  })

  it('resumes through the profile-bound requester rather than the foreground requester', async () => {
    vi.mocked(requestGatewayForProfile).mockResolvedValue({ session_id: 'runtime-work' })
    const requestGateway = vi.fn()
    renderTile(requestGateway)

    await expect(
      sessionTileDelegate()!.resumeSurface({ profile: 'work', storedSessionId: 'stored-work' })
    ).resolves.toBe('runtime-work')

    expect(requestGatewayForProfile).toHaveBeenCalledWith('work', 'session.resume', {
      session_id: 'stored-work',
      cols: 96,
      omit_messages: true,
      profile: 'work'
    })
    expect(requestGateway).not.toHaveBeenCalled()
  })

  it('purges the durable surface binding after archive so reopening cannot republish discarded runtime state', async () => {
    const stateByRuntime = { current: new Map<string, ReturnType<typeof createClientSessionState>>() }
    const archiveSession = vi.fn(async () => undefined)
    const resumed = ['runtime-archived', 'runtime-fresh']

    vi.mocked(requestGatewayForProfile).mockImplementation(async (_profile, method) =>
      method === 'session.resume' ? ({ session_id: resumed.shift() } as never) : ({} as never)
    )
    renderHook(() =>
      useSessionTileDelegate({
        archiveSession,
        branchStoredSession: vi.fn(async () => undefined),
        executeSlashCommand: vi.fn(async () => undefined) as never,
        removeSession: vi.fn(async () => undefined),
        requestGateway: vi.fn() as never,
        runtimeIdByStoredSessionIdRef: { current: new Map() },
        sessionStateByRuntimeIdRef: stateByRuntime as never,
        updateSessionState: ((runtimeId: string, updater: (state: ReturnType<typeof createClientSessionState>) => ReturnType<typeof createClientSessionState>, storedSessionId?: string, profile?: string) => {
          const key = sessionRuntimeStateKey(profile, runtimeId)
          const next = { ...updater(stateByRuntime.current.get(key) ?? createClientSessionState()), storedSessionId: storedSessionId ?? null }
          stateByRuntime.current.set(key, next)

          return next
        }) as never
      })
    )

    await expect(sessionTileDelegate()!.resumeSurface({ profile: 'work', storedSessionId: 'stored-work' })).resolves.toBe(
      'runtime-archived'
    )
    await sessionTileDelegate()!.archiveSession('stored-work', 'work')
    await expect(sessionTileDelegate()!.resumeSurface({ profile: 'work', storedSessionId: 'stored-work' })).resolves.toBe(
      'runtime-fresh'
    )

    expect(archiveSession).toHaveBeenCalledWith('stored-work')
    expect(requestGatewayForProfile).toHaveBeenCalledTimes(2)
  })
})

describe('useSessionTileDelegate interruptSession', () => {
  beforeEach(() => {
    setSessions([])
  })

  afterEach(async () => {
    setSessions([])
    const { clearSessionRecentlyInterrupted } = await import('../../session/hooks/use-prompt-actions/utils')
    clearSessionRecentlyInterrupted()
  })

  it('marks the session recently interrupted so a quick tile edit/resend still interrupt-firsts (#83855)', async () => {
    const { isSessionRecentlyInterrupted } = await import('../../session/hooks/use-prompt-actions/utils')

    const requestGateway = vi.fn(async () => ({}) as never)

    renderTile(requestGateway)
    await sessionTileDelegate()!.interruptSession('runtime-tile-1')

    expect(requestGateway).toHaveBeenCalledWith('session.interrupt', { session_id: 'runtime-tile-1' })
    expect(isSessionRecentlyInterrupted('runtime-tile-1')).toBe(true)
  })
})
