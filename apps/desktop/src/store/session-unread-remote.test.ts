import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

const patch = vi.fn<(id: string, unread: boolean, profile?: null | string) => Promise<{ ok: boolean }>>(() =>
  Promise.resolve({ ok: true })
)

vi.mock('@/hermes', () => ({
  // The store only needs the REST mutation; keep the mock minimal.
  setApiRequestProfile: () => {},
  setSessionUnreadRemote: (id: string, unread: boolean, profile?: null | string) => patch(id, unread, profile)
}))

import { $sessions } from '@/store/session'

import { $activeGatewayProfile } from './profile'
import { $unreadWriteGuard, clearUnreadOnOpen, markSessionUnread, rowFor, watchUnreadWriteGuard } from './session-unread-remote'

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

beforeEach(() => {
  $sessions.set([])
  $unreadWriteGuard.set(new Map())
  $activeGatewayProfile.set('default')
  patch.mockClear()
})

afterEach(() => {
  $sessions.set([])
  $unreadWriteGuard.set(new Map())
  $activeGatewayProfile.set('default')
})

describe('markSessionUnread', () => {
  it('optimistically paints the row, then PATCHes with the owning profile', async () => {
    $sessions.set([row('a', { profile: 'work', unread: false })])

    await markSessionUnread('a', true)

    expect(patch).toHaveBeenCalledWith('a', true, 'work')
    expect($sessions.get().find(s => s.id === 'a')?.unread).toBe(true)
  })

  it('no-ops for a runtime-only session with no persisted row', async () => {
    await markSessionUnread('ghost', true)

    expect(patch).not.toHaveBeenCalled()
  })

  it('rolls back the row and rethrows when the PATCH fails', async () => {
    $sessions.set([row('a', { unread: false })])
    patch.mockImplementationOnce(() => Promise.reject(new Error('offline')))

    await expect(markSessionUnread('a', true)).rejects.toThrow('offline')

    // The backend kept the old value, so the optimistic flip is undone and
    // the guard is released (nothing to fence a page about).
    expect($sessions.get().find(s => s.id === 'a')?.unread).toBe(false)
    expect($unreadWriteGuard.get().has('a')).toBe(false)
  })

  it('PATCHes the active-gateway profile row, not a same-id row from another profile', async () => {
    // Session ids are caller-supplied and each profile's backend is its own
    // namespace, so two profiles can hold the same id — a real, documented
    // scenario (session-unread.ts's resolveLoadedRow), and ALL_PROFILES mode
    // routinely mixes such lists together.
    $sessions.set([
      row('a', { profile: 'personal', unread: false }),
      row('a', { profile: 'work', unread: false })
    ])
    $activeGatewayProfile.set('work')

    await markSessionUnread('a', true)

    expect(patch).toHaveBeenCalledWith('a', true, 'work')
    expect(patch).toHaveBeenCalledTimes(1)
  })

  it('only optimistically paints the targeted profile row, leaving the other profile\'s same-id row untouched', async () => {
    $sessions.set([
      row('a', { profile: 'personal', unread: false }),
      row('a', { profile: 'work', unread: false })
    ])
    $activeGatewayProfile.set('work')

    await markSessionUnread('a', true)

    const rows = $sessions.get()
    expect(rows.find(s => s.id === 'a' && s.profile === 'work')?.unread).toBe(true)
    expect(rows.find(s => s.id === 'a' && s.profile === 'personal')?.unread).toBe(false)
  })

  it('only rolls back the targeted profile row on a failed PATCH', async () => {
    // 'personal' starts unread so the mid-flight check below can tell a
    // scoped optimistic paint apart from an unscoped one that also flips
    // 'personal' — checking only the POST-rollback state can't: an unscoped
    // paint-then-rollback of BOTH rows would coincidentally undo itself and
    // land on the same final values as a correctly-scoped write.
    $sessions.set([
      row('a', { profile: 'personal', unread: true }),
      row('a', { profile: 'work', unread: false })
    ])
    $activeGatewayProfile.set('work')
    let rejectPatch: (err: Error) => void = () => {}
    patch.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectPatch = reject }))

    const pending = markSessionUnread('a', true)

    // The optimistic paint runs synchronously before the first await point.
    let rows = $sessions.get()
    expect(rows.find(s => s.id === 'a' && s.profile === 'work')?.unread).toBe(true)
    expect(rows.find(s => s.id === 'a' && s.profile === 'personal')?.unread).toBe(true)

    rejectPatch(new Error('offline'))
    await expect(pending).rejects.toThrow('offline')

    // The backend kept 'work' at its old value; 'personal' must be
    // completely untouched by this call's rollback too.
    rows = $sessions.get()
    expect(rows.find(s => s.id === 'a' && s.profile === 'work')?.unread).toBe(false)
    expect(rows.find(s => s.id === 'a' && s.profile === 'personal')?.unread).toBe(true)
  })
})

describe('rowFor', () => {
  it('resolves a unique id unambiguously', () => {
    $sessions.set([row('a', { profile: 'work' })])

    expect(rowFor('a')?.profile).toBe('work')
  })

  it('breaks a same-id tie across profiles toward the active gateway profile', () => {
    $sessions.set([
      row('a', { profile: 'personal' }),
      row('a', { profile: 'work' })
    ])
    $activeGatewayProfile.set('work')

    expect(rowFor('a')?.profile).toBe('work')
  })
})

describe('clearUnreadOnOpen', () => {
  it('no-ops for a session that is already read', async () => {
    $sessions.set([row('a', { unread: false })])

    await clearUnreadOnOpen('a')

    expect(patch).not.toHaveBeenCalled()
  })

  it('PATCHes read for an unread session, using its owning profile', async () => {
    $sessions.set([row('a', { profile: 'p2', unread: true })])

    await clearUnreadOnOpen('a')

    expect(patch).toHaveBeenCalledWith('a', false, 'p2')
    expect($sessions.get().find(s => s.id === 'a')?.unread).toBe(false)
  })

  it('swallows a failed PATCH (the next honest refresh heals the dot)', async () => {
    $sessions.set([row('a', { unread: true })])
    patch.mockImplementationOnce(() => Promise.reject(new Error('offline')))

    await expect(clearUnreadOnOpen('a')).resolves.toBeUndefined()
  })
})

describe('watchUnreadWriteGuard', () => {
  it('drops a guard entry once a list page confirms the value we wrote', () => {
    watchUnreadWriteGuard()
    const guard = new Map<string, { at: number; profile: string; value: boolean }>()
    guard.set('a', { at: Date.now(), profile: 'default', value: true })
    $unreadWriteGuard.set(guard)

    // The server caught up and echoes our value back.
    $sessions.set([row('a', { unread: true })])

    expect($unreadWriteGuard.get().has('a')).toBe(false)
  })

  it('keeps the guard while a page contradicts a write still in flight', () => {
    watchUnreadWriteGuard()
    const guard = new Map<string, { at: number; profile: string; value: boolean }>()
    guard.set('a', { at: Date.now(), profile: 'default', value: true })
    $unreadWriteGuard.set(guard)

    // A list request issued before the PATCH still says read. Honouring it
    // would silently undo the mark the user just made.
    $sessions.set([row('a', { unread: false })])

    expect($unreadWriteGuard.get().has('a')).toBe(true)
  })

  it('ignores a same-id row from a DIFFERENT profile when confirming a guard', () => {
    watchUnreadWriteGuard()
    const guard = new Map<string, { at: number; profile: string; value: boolean }>()
    guard.set('a', { at: Date.now(), profile: 'work', value: true })
    $unreadWriteGuard.set(guard)

    // Same id, but a DIFFERENT profile's row confirming the SAME value --
    // must not be mistaken for the guarded write, which targeted 'work'.
    $sessions.set([row('a', { profile: 'personal', unread: true })])

    expect($unreadWriteGuard.get().has('a')).toBe(true)
  })
})
