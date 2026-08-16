import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'
import {
  $sessionStates,
  $sessionSurfaceProfiles,
  $sessionTiles,
  bindSessionSurfaceRuntime,
  publishSessionState,
  releaseSessionSurfaceReference,
  retainSessionSurfaceReference,
  sessionSurfaceReferenceCount
} from '@/store/session-states'

const state = (storedId: string, patch: Partial<ReturnType<typeof createClientSessionState>> = {}) => ({
  ...createClientSessionState(storedId),
  ...patch
})

/**
 * The embedded surface's durable identity is PROFILE + stored id, never the
 * stored id alone. These tests exercise the reference-counted surface binding
 * and its inclusion in the publish-time eviction path (without the surface,
 * a settled session's transcript is released; with it, the surface keeps the
 * runtime alive until it unmounts).
 */
describe('SessionSurface references: profile-bound durable identity', () => {
  beforeEach(() => {
    $sessionStates.set({})
    $sessionTiles.set([])
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    $sessionSurfaceProfiles.set([])
  })

  afterEach(() => {
    $sessionStates.set({})
    $sessionTiles.set([])
    $sessionSurfaceProfiles.set([])
  })

  it('counts references per profile-qualified durable identity', () => {
    retainSessionSurfaceReference('profile-a', 'stored-a')
    retainSessionSurfaceReference('profile-a', 'stored-a')
    expect(sessionSurfaceReferenceCount('profile-a', 'stored-a')).toBe(2)
    expect($sessionSurfaceProfiles.get()).toContain('profile-a')

    releaseSessionSurfaceReference('profile-a', 'stored-a')
    expect(sessionSurfaceReferenceCount('profile-a', 'stored-a')).toBe(1)

    releaseSessionSurfaceReference('profile-a', 'stored-a')
    expect(sessionSurfaceReferenceCount('profile-a', 'stored-a')).toBe(0)
    expect($sessionSurfaceProfiles.get()).not.toContain('profile-a')
  })

  it('treats the same stored id on two profiles as distinct surfaces', () => {
    retainSessionSurfaceReference('profile-a', 'stored-same')
    retainSessionSurfaceReference('profile-b', 'stored-same')

    expect(sessionSurfaceReferenceCount('profile-a', 'stored-same')).toBe(1)
    expect(sessionSurfaceReferenceCount('profile-b', 'stored-same')).toBe(1)

    releaseSessionSurfaceReference('profile-a', 'stored-same')
    releaseSessionSurfaceReference('profile-b', 'stored-same')
  })

  it('keeps a settled session an embedded surface references from eviction, and evicts on release', () => {
    retainSessionSurfaceReference('profile-a', 'stored-a')
    bindSessionSurfaceRuntime('profile-a', 'stored-a', 'rt-a')

    publishSessionState('rt-a', state('stored-a', { busy: true }))
    publishSessionState('rt-a', state('stored-a', { busy: false }))
    // A surface still references the runtime: the settled transcript stays.
    expect($sessionStates.get()['rt-a']).toBeDefined()

    releaseSessionSurfaceReference('profile-a', 'stored-a')
    // Released + settled -> the runtime is evicted, not parked forever.
    expect($sessionStates.get()['rt-a']).toBeUndefined()
  })

  it('binding a new runtime supersedes and evicts the previous surface runtime', () => {
    retainSessionSurfaceReference('profile-a', 'stored-a')
    bindSessionSurfaceRuntime('profile-a', 'stored-a', 'rt-a')
    publishSessionState('rt-a', state('stored-a', { busy: false }))
    expect($sessionStates.get()['rt-a']).toBeDefined()

    bindSessionSurfaceRuntime('profile-a', 'stored-a', 'rt-b')
    // rt-a is no longer referenced by the surface -> evicted once settled.
    expect($sessionStates.get()['rt-a']).toBeUndefined()

    releaseSessionSurfaceReference('profile-a', 'stored-a')
  })

  it('an unreferenced settled session still releases its transcript at publish time', () => {
    publishSessionState('rt-orphan', state('stored-orphan', { busy: true }))
    publishSessionState('rt-orphan', state('stored-orphan', { busy: false }))

    // No primary, tile, or surface reference -> transcript released, status kept.
    expect($sessionStates.get()['rt-orphan']?.messages).toEqual([])
    expect($sessionStates.get()['rt-orphan']).toMatchObject({ storedSessionId: 'stored-orphan', busy: false })
  })
})
