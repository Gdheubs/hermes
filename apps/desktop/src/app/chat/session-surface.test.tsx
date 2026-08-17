import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'
import {
  $sessionTiles,
  releaseSessionSurfaceReference,
  sessionSurfaceReferenceCount,
  setSessionSurfaceDelegate
} from '@/store/session-states'

import { SessionSurface, SessionSurfaceCore } from './session-surface'

const gatewaySubscriptions = vi.hoisted(() => ({
  all: new Set<(profile: string) => void>(),
  byProfile: new Map<string, Set<() => void>>(),
  states: new Map<string, string>()
}))

vi.mock('@/store/gateway', () => ({
  profileGatewayState: (profile: string) => ({ connectionState: gatewaySubscriptions.states.get(profile) ?? 'open' }),
  subscribeProfileGateway: (profile: string, listener: () => void) => {
    const listeners = gatewaySubscriptions.byProfile.get(profile) ?? new Set<() => void>()
    listeners.add(listener)
    gatewaySubscriptions.byProfile.set(profile, listeners)

    return () => listeners.delete(listener)
  },
  subscribeProfileGateways: (listener: (profile: string) => void) => {
    gatewaySubscriptions.all.add(listener)

    return () => gatewaySubscriptions.all.delete(listener)
  }
}))

function emitGatewayTransition(profile: string, state = 'open') {
  gatewaySubscriptions.states.set(profile, state)
  gatewaySubscriptions.all.forEach(listener => listener(profile))
  gatewaySubscriptions.byProfile.get(profile)?.forEach(listener => listener())
}

vi.mock('./session-surface-chat', () => ({
  SessionSurfaceChat: ({ runtimeSessionId }: { runtimeSessionId: string }) => (
    <div data-testid="surface-chat">{runtimeSessionId}</div>
  )
}))

const delegate = () => ({
  adoptSurface: vi.fn(async (identity: { runtimeSessionId: string }) => identity.runtimeSessionId),
  archiveSession: vi.fn(async () => undefined),
  branchSession: vi.fn(async () => undefined),
  deleteSession: vi.fn(async () => undefined),
  executeSlash: vi.fn(async () => undefined),
  interruptSession: vi.fn(async () => undefined),
  resumeSurface: vi.fn(async (_identity: { profile: string; storedSessionId: string }) => 'runtime-resumed'),
  resumeTile: vi.fn(async () => 'runtime-resumed'),
  submitToSession: vi.fn(async () => undefined),
  updateSession: vi.fn()
})

function deferred<T>() {
  let reject!: (reason?: unknown) => void
  let resolve!: (value: T | PromiseLike<T>) => void

  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    reject = rejectPromise
    resolve = resolvePromise
  })

  return { promise, reject, resolve }
}

function staleAdoptionError(message = 'Session surface identity mismatch') {
  return Object.assign(new Error(message), { name: 'StaleSessionSurfaceRuntimeError' })
}

describe('SessionSurface', () => {
  beforeEach(() => {
    window.location.hash = '#/roadmaps'
    $activeSessionId.set('runtime-primary')
    $selectedStoredSessionId.set('stored-primary')
    $activeGatewayProfile.set('default')
    $sessionTiles.set([{ storedSessionId: 'tile-primary', runtimeId: 'runtime-tile' }])
  })

  afterEach(() => {
    setSessionSurfaceDelegate(null)
    gatewaySubscriptions.all.clear()
    gatewaySubscriptions.byProfile.clear()
    gatewaySubscriptions.states.clear()
  })

  it('adopts a fresh runtime hint without resume or foreground/layout navigation mutation', async () => {
    const installed = delegate()
    setSessionSurfaceDelegate(installed)
    const beforeTiles = $sessionTiles.get()

    render(<SessionSurface session={{ profile: 'work', runtimeSessionId: 'runtime-fresh', storedSessionId: 'stored-fresh' }} />)

    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-fresh')
    expect(installed.adoptSurface).toHaveBeenCalledWith({
      profile: 'work',
      runtimeSessionId: 'runtime-fresh',
      storedSessionId: 'stored-fresh'
    })
    expect(installed.resumeSurface).not.toHaveBeenCalled()
    expect(window.location.hash).toBe('#/roadmaps')
    expect($activeSessionId.get()).toBe('runtime-primary')
    expect($selectedStoredSessionId.get()).toBe('stored-primary')
    expect($activeGatewayProfile.get()).toBe('default')
    expect($sessionTiles.get()).toBe(beforeTiles)
  })

  it('falls back once to the durable identity when a runtime hint is stale', async () => {
    const adoption = deferred<string>()
    const resume = deferred<string>()
    const installed = delegate()
    installed.adoptSurface.mockImplementationOnce(() => adoption.promise)
    installed.resumeSurface.mockImplementationOnce(() => resume.promise)
    setSessionSurfaceDelegate(installed)

    render(<SessionSurface session={{ profile: 'profile-b', runtimeSessionId: 'runtime-stale', storedSessionId: 'stored-b' }} />)
    await waitFor(() => expect(installed.adoptSurface).toHaveBeenCalledTimes(1))

    await act(async () => adoption.reject(staleAdoptionError()))
    await waitFor(() =>
      expect(installed.resumeSurface).toHaveBeenCalledWith({ profile: 'profile-b', storedSessionId: 'stored-b' })
    )

    act(() => emitGatewayTransition('profile-c', 'open'))
    expect(installed.adoptSurface).toHaveBeenCalledTimes(1)
    expect(installed.resumeSurface).toHaveBeenCalledTimes(1)

    await act(async () => resume.resolve('runtime-recovered'))
    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-recovered')
  })

  it('falls back durably when a previously adopted hint disappears after reconnect', async () => {
    const installed = delegate()
    installed.adoptSurface
      .mockResolvedValueOnce('runtime-hint')
      .mockRejectedValueOnce(staleAdoptionError('4007 Session not found'))
    installed.resumeSurface.mockResolvedValueOnce('runtime-recovered')
    setSessionSurfaceDelegate(installed)

    render(<SessionSurface session={{ profile: 'profile-b', runtimeSessionId: 'runtime-hint', storedSessionId: 'stored-b' }} />)
    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-hint')

    act(() => {
      emitGatewayTransition('profile-b', 'closed')
      emitGatewayTransition('profile-b', 'open')
    })

    await waitFor(() => expect(installed.resumeSurface).toHaveBeenCalledTimes(1))
    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-recovered')
    expect(installed.adoptSurface).toHaveBeenCalledTimes(2)
  })

  it('retries the durable recovery without adopting the known-stale hint forever', async () => {
    const installed = delegate()
    installed.adoptSurface.mockRejectedValueOnce(staleAdoptionError())
    installed.resumeSurface.mockRejectedValueOnce(new Error('temporary timeout')).mockResolvedValueOnce('runtime-recovered')
    setSessionSurfaceDelegate(installed)

    render(<SessionSurface session={{ profile: 'profile-b', runtimeSessionId: 'runtime-stale', storedSessionId: 'stored-b' }} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))

    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-recovered')
    expect(installed.adoptSurface).toHaveBeenCalledTimes(1)
    expect(installed.resumeSurface).toHaveBeenCalledTimes(2)
  })

  it('does not turn transient or security adoption errors into a durable resume', async () => {
    const installed = delegate()
    installed.adoptSurface.mockRejectedValueOnce(new Error('403 forbidden'))
    setSessionSurfaceDelegate(installed)

    render(<SessionSurface session={{ profile: 'profile-b', runtimeSessionId: 'runtime-hint', storedSessionId: 'stored-b' }} />)

    expect(await screen.findByRole('button', { name: 'Retry' })).toBeTruthy()
    expect(installed.adoptSurface).toHaveBeenCalledTimes(1)
    expect(installed.resumeSurface).not.toHaveBeenCalled()
  })

  it('discards a stale fallback completion after the durable identity changes', async () => {
    const oldResume = deferred<string>()
    const installed = delegate()
    const onRuntimeSessionId = vi.fn()
    installed.adoptSurface.mockRejectedValueOnce(staleAdoptionError())
    installed.resumeSurface.mockImplementation(({ storedSessionId }: { storedSessionId: string }) =>
      storedSessionId === 'stored-a' ? oldResume.promise : Promise.resolve('runtime-b')
    )
    setSessionSurfaceDelegate(installed)

    const mounted = render(
      <SessionSurfaceCore
        onRuntimeSessionId={onRuntimeSessionId}
        profile="profile-a"
        runtimeSessionId="runtime-stale"
        storedSessionId="stored-a"
      />
    )

    await waitFor(() =>
      expect(installed.resumeSurface).toHaveBeenCalledWith({ profile: 'profile-a', storedSessionId: 'stored-a' })
    )

    mounted.rerender(
      <SessionSurfaceCore onRuntimeSessionId={onRuntimeSessionId} profile="profile-b" storedSessionId="stored-b" />
    )
    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-b')

    await act(async () => oldResume.resolve('runtime-stale-completion'))
    expect(screen.getByTestId('surface-chat').textContent).toBe('runtime-b')
    expect(onRuntimeSessionId).toHaveBeenCalledTimes(1)
    expect(onRuntimeSessionId).toHaveBeenCalledWith('runtime-b')
  })

  it('resumes durably with the explicit profile and hydrates without navigation', async () => {
    const installed = delegate()
    setSessionSurfaceDelegate(installed)

    render(<SessionSurface session={{ profile: 'ai-engineer', storedSessionId: 'stored-existing' }} />)

    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-resumed')
    expect(installed.resumeSurface).toHaveBeenCalledWith({
      profile: 'ai-engineer',
      storedSessionId: 'stored-existing'
    })
    expect(installed.adoptSurface).not.toHaveBeenCalled()
    expect(window.location.hash).toBe('#/roadmaps')
    expect($activeGatewayProfile.get()).toBe('default')
  })

  it('releases only its local reference and remounts from the durable identity without interrupting', async () => {
    const installed = delegate()
    setSessionSurfaceDelegate(installed)

    const first = render(
      <SessionSurface session={{ profile: 'work', runtimeSessionId: 'runtime-fresh', storedSessionId: 'stored-fresh' }} />
    )

    await screen.findByTestId('surface-chat')
    expect(sessionSurfaceReferenceCount('work', 'stored-fresh')).toBe(1)

    act(() => first.unmount())
    expect(sessionSurfaceReferenceCount('work', 'stored-fresh')).toBe(0)
    expect(installed.interruptSession).not.toHaveBeenCalled()

    render(<SessionSurface session={{ profile: 'work', storedSessionId: 'stored-fresh' }} />)
    await waitFor(() => expect(installed.resumeSurface).toHaveBeenCalledWith({ profile: 'work', storedSessionId: 'stored-fresh' }))
    expect(sessionSurfaceReferenceCount('work', 'stored-fresh')).toBe(1)

    // Defensive cleanup in case a failed assertion leaves a retained token.
    releaseSessionSurfaceReference('work', 'stored-fresh')
  })

  it('waits for a late delegate instead of latching unavailable at cold start', async () => {
    render(<SessionSurface session={{ profile: 'work', storedSessionId: 'stored-late' }} />)
    expect(screen.queryByText(/unavailable/i)).toBeNull()

    const installed = delegate()
    setSessionSurfaceDelegate(installed)

    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-resumed')
    expect(installed.resumeSurface).toHaveBeenCalledWith({ profile: 'work', storedSessionId: 'stored-late' })
  })

  it('starts only one resume while its profile opens and another profile transitions', async () => {
    let resolveResume!: (runtimeSessionId: string) => void
    const installed = delegate()
    installed.resumeSurface.mockImplementationOnce(() => new Promise(resolve => (resolveResume = resolve)))
    setSessionSurfaceDelegate(installed)

    render(<SessionSurface session={{ profile: 'profile-b', storedSessionId: 'stored-b' }} />)
    await waitFor(() => expect(installed.resumeSurface).toHaveBeenCalledTimes(1))

    act(() => {
      emitGatewayTransition('profile-b')
      emitGatewayTransition('profile-c')
    })

    expect(installed.resumeSurface).toHaveBeenCalledTimes(1)

    resolveResume('runtime-b')
    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-b')
  })

  it('discards an in-flight completion across connection loss and retries only after it settles', async () => {
    let resolveFirst!: (runtimeSessionId: string) => void
    let resolveSecond!: (runtimeSessionId: string) => void
    const installed = delegate()
    installed.resumeSurface
      .mockImplementationOnce(() => new Promise(resolve => (resolveFirst = resolve)))
      .mockImplementationOnce(() => new Promise(resolve => (resolveSecond = resolve)))
    setSessionSurfaceDelegate(installed)

    render(<SessionSurface session={{ profile: 'profile-b', storedSessionId: 'stored-b' }} />)
    await waitFor(() => expect(installed.resumeSurface).toHaveBeenCalledTimes(1))

    act(() => {
      emitGatewayTransition('profile-b', 'closed')
      emitGatewayTransition('profile-b', 'open')
    })
    expect(installed.resumeSurface).toHaveBeenCalledTimes(1)

    act(() => resolveFirst('runtime-stale'))
    await waitFor(() => expect(installed.resumeSurface).toHaveBeenCalledTimes(2))
    expect(screen.queryByText('runtime-stale')).toBeNull()

    resolveSecond('runtime-fresh')
    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-fresh')
  })

  it.each([
    { profile: ' ', storedSessionId: 'stored', runtimeSessionId: undefined },
    { profile: 'work\u0000evil', storedSessionId: 'stored', runtimeSessionId: undefined },
    { profile: 'work', storedSessionId: '\n', runtimeSessionId: undefined },
    { profile: 'work', storedSessionId: 'stored', runtimeSessionId: 'runtime\u0007evil' }
  ])('rejects invalid public identities before retain or adoption: %o', async props => {
    const installed = delegate()
    setSessionSurfaceDelegate(installed)

    render(<SessionSurface session={props} />)

    expect(await screen.findByText("Couldn't open this session")).toBeTruthy()
    expect(installed.adoptSurface).not.toHaveBeenCalled()
    expect(installed.resumeSurface).not.toHaveBeenCalled()
    expect(sessionSurfaceReferenceCount(props.profile, props.storedSessionId)).toBe(0)
  })

  it('redacts backend failures and retries when delegate readiness changes', async () => {
    const failing = delegate()
    failing.resumeSurface.mockRejectedValueOnce(new Error('/home/secret/state.db provider token=abc'))
    setSessionSurfaceDelegate(failing)

    render(<SessionSurface session={{ profile: 'work', storedSessionId: 'stored-retry' }} />)
    expect(await screen.findByText("Couldn't open this session")).toBeTruthy()
    expect(screen.queryByText(/secret|token=abc/)).toBeNull()

    const recovered = delegate()
    setSessionSurfaceDelegate(recovered)
    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-resumed')
  })

  it('offers an explicit retry after a transient resume failure on the same delegate', async () => {
    const installed = delegate()
    installed.resumeSurface.mockRejectedValueOnce(new Error('temporary timeout'))
    setSessionSurfaceDelegate(installed)

    render(<SessionSurface session={{ profile: 'work', storedSessionId: 'stored-retry-button' }} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))

    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-resumed')
    expect(installed.resumeSurface).toHaveBeenCalledTimes(2)
  })

  it('never paints the previous transcript while its explicit durable identity changes', async () => {
    const installed = delegate()
    let resolveSecond!: (runtimeSessionId: string) => void

    installed.resumeSurface
      .mockResolvedValueOnce('runtime-one')
      .mockImplementationOnce(() => new Promise(resolve => (resolveSecond = resolve)))
    setSessionSurfaceDelegate(installed)

    const mounted = render(<SessionSurface session={{ profile: 'work', storedSessionId: 'stored-one' }} />)
    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-one')

    mounted.rerender(<SessionSurface session={{ profile: 'other', storedSessionId: 'stored-two' }} />)
    expect(screen.queryByTestId('surface-chat')).toBeNull()

    resolveSecond('runtime-two')
    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-two')
  })

  it('does not carry a prior profile connection loss into a new identity', async () => {
    const installed = delegate()
    installed.resumeSurface.mockResolvedValueOnce('runtime-a').mockResolvedValueOnce('runtime-b')
    setSessionSurfaceDelegate(installed)

    const mounted = render(<SessionSurface session={{ profile: 'profile-a', storedSessionId: 'stored-a' }} />)
    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-a')
    act(() => emitGatewayTransition('profile-a', 'closed'))

    mounted.rerender(<SessionSurface session={{ profile: 'profile-b', storedSessionId: 'stored-b' }} />)
    expect((await screen.findByTestId('surface-chat')).textContent).toBe('runtime-b')
    act(() => emitGatewayTransition('profile-b', 'open'))

    expect(installed.resumeSurface).toHaveBeenCalledTimes(2)
  })
})
