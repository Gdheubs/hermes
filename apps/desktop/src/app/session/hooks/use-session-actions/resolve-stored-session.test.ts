import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesModule from '@/hermes'
import { getSession } from '@/hermes'
import { $activeGatewayProfile, $profiles } from '@/store/profile'
import { $cronSessions, $messagingSessions, $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { resolveSessionProfile, resolveStoredSession } from './utils'

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<typeof HermesModule>()),
  getSession: vi.fn()
}))

const mockGetSession = vi.mocked(getSession)

const session = (over: Partial<SessionInfo>): SessionInfo => over as SessionInfo

/**
 * Parsed JSON shaped like a `getSession` payload, not a Partial<SessionInfo>
 * object literal. A single `as SessionInfo` would let the compiler treat
 * `message_count: "0"` or `source: " Unknown "` as already matching the
 * declared types. JSON round-trip preserves those runtime types; the
 * `unknown` → `SessionInfo` assertion is the resolver boundary the tests
 * are proving, not a fixture lie.
 */
const sessionFromRuntimeJson = (payload: Record<string, unknown>): SessionInfo =>
  JSON.parse(JSON.stringify(payload)) as unknown as SessionInfo

const profiles = (...names: string[]) => names.map(name => ({ name }) as never)

describe('resolveStoredSession profile ownership', () => {
  beforeEach(() => {
    $cronSessions.set([])
    $messagingSessions.set([])
    $sessions.set([])
    $profiles.set(profiles('default', 'meta'))
    $activeGatewayProfile.set('meta')
    mockGetSession.mockReset()
  })

  afterEach(() => {
    $cronSessions.set([])
    $messagingSessions.set([])
    $sessions.set([])
    $profiles.set([])
    $activeGatewayProfile.set('default')
  })

  it('returns a cached row that carries an owning profile', async () => {
    $sessions.set([session({ id: 's1', profile: 'default' })])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it.each([
    ['cron', $cronSessions],
    ['messaging', $messagingSessions]
  ])('resolves a %s sidebar row without duplicating it into regular sessions', async (_source, store) => {
    store.set([session({ id: 's1', profile: 'default' })])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    expect(mockGetSession).not.toHaveBeenCalled()
    expect($sessions.get()).toEqual([])
  })

  it('treats a profile-less cache hit as unresolved when multiple profiles exist', async () => {
    $sessions.set([session({ id: 's1' })])
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's1', profile: 'default' }))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    // rung 2 (bare) then rung 3 (stamped cross-profile probe)
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
  })

  it('accepts a profile-less cache hit for single-profile users', async () => {
    $profiles.set(profiles('default'))
    $sessions.set([session({ id: 's1' })])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.id).toBe('s1')
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('stamps the active profile on a bare by-id hit from an older backend', async () => {
    mockGetSession.mockResolvedValueOnce(session({ id: 's1' }))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('meta')
    // the upserted cache row is owned too, so the next hit short-circuits
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('meta')
  })

  it('probed desktop profile overrides a remote backend answering as its own "default"', async () => {
    // Per-profile remote override: Electron strips the desktop alias before
    // forwarding, so the standalone backend stamps its backend-local root.
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's1', profile: 'default' }))
    $activeGatewayProfile.set('default')
    $profiles.set(profiles('default', 'meta'))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('meta')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('meta')
  })

  it('stamps the probed profile on a scoped hit from an older backend that omits it', async () => {
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's1' }))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    // the cached row is owned too — no unowned row is ever re-cached
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('default')
  })

  it('resolveSessionProfile routes a default-profile session from a non-default gateway', async () => {
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's1', profile: 'default' }))

    await expect(resolveSessionProfile('s1')).resolves.toBe('default')
  })

  it('skips an owning empty unknown shadow when another profile owns a materialized twin', async () => {
    // Legacy damage: active profile (meta/production) holds a titled empty
    // source=unknown shadow for the same id that default owns as a real desktop row.
    const emptyShadow = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: 'Real conversation'
    })

    const materialized = session({
      id: 's1',
      message_count: 4,
      profile: 'default',
      source: 'desktop',
      title: 'Real conversation'
    })

    $sessions.set([emptyShadow])
    mockGetSession.mockResolvedValueOnce(emptyShadow)
    mockGetSession.mockResolvedValueOnce(materialized)

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    expect(resolved?.message_count).toBe(4)
    expect(resolved?.source).toBe('desktop')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('default')
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
  })

  it('recovers a materialized twin when the uncached active-backend hit is the legacy shadow', async () => {
    const activeShadow = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: 'Real conversation'
    })

    const materialized = session({
      id: 's1',
      message_count: 4,
      profile: 'default',
      source: 'desktop',
      title: 'Real conversation'
    })

    mockGetSession.mockResolvedValueOnce(activeShadow)
    mockGetSession.mockResolvedValueOnce(materialized)

    const resolved = await resolveStoredSession('s1')

    expect(resolved).toBe(materialized)
    expect($sessions.get()[0]).toBe(materialized)
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
  })

  it('recovers a later materialized twin when the first scoped probe yields the legacy shadow', async () => {
    $profiles.set(profiles('meta', 'other', 'default'))

    const probedShadow = session({
      id: 's1',
      message_count: 0,
      profile: 'other',
      source: 'unknown',
      title: 'Real conversation'
    })

    const materialized = session({
      id: 's1',
      message_count: 4,
      profile: 'default',
      source: 'desktop',
      title: 'Real conversation'
    })

    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(probedShadow)
    mockGetSession.mockResolvedValueOnce(materialized)

    const resolved = await resolveStoredSession('s1')

    expect(resolved).toBe(materialized)
    expect($sessions.get()[0]).toBe(materialized)
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'other')
    expect(mockGetSession).toHaveBeenNthCalledWith(3, 's1', 'default')
  })

  it('returns and upserts an uncached backend shadow when no profile owns a twin', async () => {
    $profiles.set(profiles('meta', 'other', 'default'))

    const newer = session({ id: 'newer', profile: 'meta' })

    const activeShadow = sessionFromRuntimeJson({
      id: 's1',
      message_count: ' 0 ',
      profile: '   ',
      source: ' Unknown ',
      title: 'Real conversation'
    })

    const older = session({ id: 'older', profile: 'default' })

    $sessions.set([newer, older])
    mockGetSession.mockResolvedValueOnce(activeShadow)
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('meta')
    expect(resolved?.source).toBe('unknown')
    expect(resolved?.message_count).toBe(0)
    expect($sessions.get().map(row => row.id)).toEqual(['s1', 'newer', 'older'])
    expect($sessions.get()[0]).toBe(resolved)
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'other')
    expect(mockGetSession).toHaveBeenNthCalledWith(3, 's1', 'default')
  })

  it('lets a positive string message_count win as the transcript-bearing owner', async () => {
    const emptyShadow = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: 'Real conversation'
    })

    const materialized = sessionFromRuntimeJson({
      id: 's1',
      message_count: '4',
      profile: 'default',
      source: 'desktop',
      title: 'Real conversation'
    })

    $sessions.set([emptyShadow])
    mockGetSession.mockResolvedValueOnce(emptyShadow)
    mockGetSession.mockResolvedValueOnce(materialized)

    const resolved = await resolveStoredSession('s1')

    expect(resolved).toBe(materialized)
    expect(resolved?.profile).toBe('default')
    expect(resolved?.message_count).toBe('4')
    expect($sessions.get()[0]).toBe(materialized)
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
  })

  it('keeps probing past an earlier zero-message known-source for a transcript-bearing twin', async () => {
    // Profile order meta → other → default: a known-source zero-message row
    // (desktop/0) is neither returned nor remembered. It is discarded, and
    // probing continues so a later transcript-bearing twin wins immediately.
    $profiles.set(profiles('meta', 'other', 'default'))
    $activeGatewayProfile.set('meta')

    const emptyShadow = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: 'Real conversation'
    })

    const zeroMessageOther = session({
      id: 's1',
      message_count: 0,
      profile: 'other',
      source: 'desktop',
      title: 'Real conversation'
    })

    const materialized = session({
      id: 's1',
      message_count: 4,
      profile: 'default',
      source: 'desktop',
      title: 'Real conversation'
    })

    $sessions.set([emptyShadow])
    mockGetSession.mockResolvedValueOnce(emptyShadow)
    mockGetSession.mockResolvedValueOnce(zeroMessageOther)
    mockGetSession.mockResolvedValueOnce(materialized)

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    expect(resolved?.message_count).toBe(4)
    expect(resolved?.source).toBe('desktop')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('default')
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'other')
    expect(mockGetSession).toHaveBeenNthCalledWith(3, 's1', 'default')
  })

  it('preserves the cached unknown/0 owner when a competing titled tui/0 draft has no transcript', async () => {
    // Zero/zero collision: the active profile owns a titled unknown/0 shadow,
    // and another profile has a legitimate titled tui/0 draft for the same id.
    // Source alone is not ownership evidence — keep the original cached owner.
    $profiles.set(profiles('meta', 'other', 'default'))
    $activeGatewayProfile.set('meta')

    const emptyShadow = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: 'Planned conversation'
    })

    const competingDraft = session({
      id: 's1',
      message_count: 0,
      profile: 'other',
      source: 'tui',
      title: 'Planned conversation'
    })

    $sessions.set([emptyShadow])
    mockGetSession.mockResolvedValueOnce(emptyShadow)
    mockGetSession.mockResolvedValueOnce(competingDraft)
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('meta')
    expect(resolved?.message_count).toBe(0)
    expect(resolved?.source).toBe('unknown')
    expect(resolved?.title).toBe('Planned conversation')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('meta')
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'other')
    expect(mockGetSession).toHaveBeenNthCalledWith(3, 's1', 'default')
  })

  it('preserves the cached unknown/0 owner when a competing titled desktop/0 draft has no transcript', async () => {
    // Same zero/zero collision as the tui/0 case: a legitimate desktop/0 titled
    // draft on another profile is not independent ownership evidence.
    $profiles.set(profiles('meta', 'other', 'default'))
    $activeGatewayProfile.set('meta')

    const emptyShadow = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: 'Untitled draft'
    })

    const zeroMessageOther = session({
      id: 's1',
      message_count: 0,
      profile: 'other',
      source: 'desktop',
      title: 'Untitled draft'
    })

    $sessions.set([emptyShadow])
    mockGetSession.mockResolvedValueOnce(emptyShadow)
    mockGetSession.mockResolvedValueOnce(zeroMessageOther)
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('meta')
    expect(resolved?.message_count).toBe(0)
    expect(resolved?.source).toBe('unknown')
    expect(resolved?.title).toBe('Untitled draft')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('meta')
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'other')
    expect(mockGetSession).toHaveBeenNthCalledWith(3, 's1', 'default')
  })

  it('keeps a titled empty unknown row when no other profile owns a materialized twin', async () => {
    const newer = session({ id: 'newer', profile: 'meta' })

    const emptyDraft = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: 'Untitled draft'
    })

    const older = session({ id: 'older', profile: 'default' })

    $sessions.set([newer, emptyDraft, older])
    mockGetSession.mockResolvedValueOnce(emptyDraft)
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('meta')
    expect(resolved?.message_count).toBe(0)
    expect(resolved?.source).toBe('unknown')
    expect(resolved?.title).toBe('Untitled draft')
    expect($sessions.get().map(row => row.id)).toEqual(['s1', 'newer', 'older'])
    expect($sessions.get()[0]).toBe(emptyDraft)
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
  })

  it('returns a single-profile empty unknown draft from cache without probing', async () => {
    // No cross-profile twin can exist, so the exact legacy shadow shape must
    // not force a getSession round-trip.
    $profiles.set(profiles('default'))
    $activeGatewayProfile.set('default')
    $sessions.set([
      session({
        id: 's1',
        message_count: 0,
        profile: 'default',
        source: 'unknown',
        title: 'Untitled draft'
      })
    ])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.id).toBe('s1')
    expect(resolved?.source).toBe('unknown')
    expect(resolved?.message_count).toBe(0)
    expect(resolved?.title).toBe('Untitled draft')
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('does not treat an untitled empty unknown row as the legacy title shadow', async () => {
    // The observed legacy damage is a title-generation stub: source=unknown,
    // message_count=0, and a persisted title. An untitled auxiliary/accounting
    // row is ambiguous and must not be retargeted to a same-id session owned by
    // another profile.
    const ambiguous = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: null
    })

    $sessions.set([ambiguous])
    mockGetSession.mockResolvedValueOnce(
      session({
        id: 's1',
        message_count: 4,
        profile: 'default',
        source: 'desktop',
        title: 'Different conversation'
      })
    )

    const resolved = await resolveStoredSession('s1')

    expect(resolved).toBe(ambiguous)
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('does not treat a whitespace-only title as the legacy title shadow', async () => {
    const ambiguous = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'unknown',
      title: '   '
    })

    $sessions.set([ambiguous])

    const resolved = await resolveStoredSession('s1')

    expect(resolved).toBe(ambiguous)
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('keeps a titled known-source zero-message draft from cache without probing', async () => {
    // `/title` before the first prompt intentionally creates desktop/0 or
    // tui/0 rows. A title alone does not make a row a legacy shadow.
    const draft = session({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: 'desktop',
      title: 'Planned conversation'
    })

    $sessions.set([draft])

    const resolved = await resolveStoredSession('s1')

    expect(resolved).toBe(draft)
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('does not treat omitted message_count as the legacy empty shadow', async () => {
    // Only message_count === 0 is the damaged shape; undefined must keep the
    // direct cache short-circuit even under multi-profile.
    const ambiguous = session({
      id: 's1',
      profile: 'meta',
      source: 'unknown',
      title: 'Maybe a draft'
    })

    $sessions.set([ambiguous])

    const resolved = await resolveStoredSession('s1')

    expect(resolved).toBe(ambiguous)
    expect(resolved?.message_count).toBeUndefined()
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('defers a titled unknown/0 shadow when source has surrounding whitespace and mixed case', async () => {
    const emptyShadow = sessionFromRuntimeJson({
      id: 's1',
      message_count: 0,
      profile: 'meta',
      source: ' Unknown ',
      title: 'Real conversation'
    })

    const materialized = session({
      id: 's1',
      message_count: 4,
      profile: 'default',
      source: 'desktop',
      title: 'Real conversation'
    })

    $sessions.set([emptyShadow])
    mockGetSession.mockResolvedValueOnce(emptyShadow)
    mockGetSession.mockResolvedValueOnce(materialized)

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    expect(resolved?.message_count).toBe(4)
    expect(resolved?.source).toBe('desktop')
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
  })

  it('defers a titled unknown/0 shadow when message_count is the numeric string "0"', async () => {
    const emptyShadow = sessionFromRuntimeJson({
      id: 's1',
      message_count: '0',
      profile: 'meta',
      source: 'unknown',
      title: 'Real conversation'
    })

    const materialized = session({
      id: 's1',
      message_count: 4,
      profile: 'default',
      source: 'desktop',
      title: 'Real conversation'
    })

    $sessions.set([emptyShadow])
    mockGetSession.mockResolvedValueOnce(emptyShadow)
    mockGetSession.mockResolvedValueOnce(materialized)

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    expect(resolved?.message_count).toBe(4)
    expect(resolved?.source).toBe('desktop')
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
  })

  it('defers a titled unknown/0 shadow when message_count is a padded numeric string zero', async () => {
    const emptyShadow = sessionFromRuntimeJson({
      id: 's1',
      message_count: ' 0 ',
      profile: 'meta',
      source: 'unknown',
      title: 'Real conversation'
    })

    const materialized = session({
      id: 's1',
      message_count: 4,
      profile: 'default',
      source: 'desktop',
      title: 'Real conversation'
    })

    $sessions.set([emptyShadow])
    mockGetSession.mockResolvedValueOnce(emptyShadow)
    mockGetSession.mockResolvedValueOnce(materialized)

    const resolved = await resolveStoredSession('s1')

    expect(resolved).toBe(materialized)
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
  })

  it('trims surrounding whitespace on a cached row profile without changing case', async () => {
    $profiles.set(profiles('MetaProd', 'default'))
    $activeGatewayProfile.set('default')
    $sessions.set([session({ id: 's1', profile: '  MetaProd  ' })])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('MetaProd')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('  MetaProd  ')
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('stamps the active gateway profile on an unscoped hit whose profile is only whitespace', async () => {
    mockGetSession.mockResolvedValueOnce(
      sessionFromRuntimeJson({
        id: 's1',
        profile: '   '
      })
    )

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('meta')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('meta')
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
  })

  it('normalizes a malformed cache hit without reordering or dropping unrelated rows', async () => {
    $profiles.set(profiles('MetaProd', 'default'))
    $activeGatewayProfile.set('default')

    const newer = session({ id: 'newer', profile: 'default', source: 'desktop' })

    const target = sessionFromRuntimeJson({
      id: 's1',
      profile: '  MetaProd  ',
      source: ' Desktop ',
      title: 'Cached conversation'
    })

    const older = session({ id: 'older', profile: 'default', source: 'tui' })

    $sessions.set([newer, target, older])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.id).toBe('s1')
    expect(resolved?.profile).toBe('MetaProd')
    expect(resolved?.source).toBe('Desktop')
    expect($sessions.get().map(row => row.id)).toEqual(['newer', 's1', 'older'])
    expect($sessions.get()[0]).toBe(newer)
    expect($sessions.get()[1]).toBe(target)
    expect($sessions.get()[2]).toBe(older)
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('does not promote a normalized cache hit to the front of $sessions', async () => {
    const newer = session({ id: 'newer', profile: 'meta' })

    const target = sessionFromRuntimeJson({
      id: 's1',
      profile: '  meta  ',
      source: 'cli'
    })

    const older = session({ id: 'older', profile: 'default' })

    $sessions.set([newer, target, older])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('meta')
    expect($sessions.get()[0]?.id).toBe('newer')
    expect($sessions.get()[0]).toBe(newer)
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('returns the active gateway profile for a single-profile cached blank profile without rewriting cache', async () => {
    $profiles.set(profiles('meta'))
    $activeGatewayProfile.set('meta')

    const newer = session({ id: 'newer', profile: 'meta' })

    const target = sessionFromRuntimeJson({
      id: 's1',
      profile: '   '
    })

    const older = session({ id: 'older', profile: 'meta' })

    $sessions.set([newer, target, older])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.id).toBe('s1')
    expect(resolved?.profile).toBe('meta')
    expect($sessions.get().map(row => row.id)).toEqual(['newer', 's1', 'older'])
    expect($sessions.get()[1]).toBe(target)
    expect($sessions.get()[1]?.profile).toBe('   ')
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('does not treat message_count "00" or "0.0" as the legacy empty shadow', async () => {
    for (const messageCount of ['00', '0.0'] as const) {
      const row = sessionFromRuntimeJson({
        id: 's1',
        message_count: messageCount,
        profile: 'meta',
        source: 'unknown',
        title: 'Maybe a draft'
      })

      $sessions.set([row])
      mockGetSession.mockClear()

      const resolved = await resolveStoredSession('s1')

      expect(resolved).toBe(row)
      expect(resolved?.message_count).toBe(messageCount)
      expect(mockGetSession).not.toHaveBeenCalled()
    }
  })

  it('keeps scoped probe ownership when the backend row profile is blank', async () => {
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(
      sessionFromRuntimeJson({
        id: 's1',
        profile: '   '
      })
    )

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('default')
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
  })
})
