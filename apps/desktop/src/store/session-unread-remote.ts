/**
 * Persisted unread flag sync (backend read-state watermark via
 * PATCH /api/sessions/{id} → SessionDB.set_session_read).
 *
 * The sidebar's dot is fed by TWO sources (see session-dot-state.ts): the
 * runtime "turn finished in background" marker ($unreadFinishedSessionIds,
 * transient) and the backend's derived `unread` key (last_read_at watermark
 * vs last_active — survives restarts and is visible to every surface). This
 * module owns the WRITE side of the persisted flag: the row-level
 * "Mark as unread"/"Mark as read" toggle and the automatic clear when a
 * session is opened. The read side lives in session-dot-state.ts.
 *
 * Optimistic, then honest (AGENTS.md): paint the row immediately, PATCH the
 * backend, roll back visibly on failure. A list page already in flight when
 * we PATCH can land after the ack carrying the OLD value — the write guard
 * lets our value outrank that stale page briefly (#74570 pattern, same as
 * session-pin-sync.ts).
 *
 * NOTE: import cycle with ./session is inert — both modules only touch each
 * other's exports inside function bodies, never at module evaluation time.
 */
import { atom } from 'nanostores'

import { setSessionUnreadRemote } from '@/hermes'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'

import { $sessions, setSessions } from './session'

export const UNREAD_WRITE_GUARD_MS = 10_000

/** id -> the value we wrote, when, and which profile's row it targeted.
 *  Guarded rows outrank list pages. Profile is carried so a same-id row in
 *  ANOTHER profile can never be mistaken for the one this guard is fencing. */
export const $unreadWriteGuard = atom<Map<string, { at: number; profile: string; value: boolean }>>(new Map())

/** The row a bare stored id refers to. Ids are caller-supplied and each
 *  profile's backend is its own namespace, so two profiles can hold the same
 *  id (session-unread.ts's resolveLoadedRow documents this same fact for the
 *  watermark/marker half of the unread feature) — a tie breaks toward the
 *  live gateway, since both opening a session and toggling its flag from the
 *  row menu only ever happen against what's currently on screen. */
export function rowFor(storedId: string) {
  const matches = $sessions.get().filter(row => row.id === storedId)

  if (matches.length < 2) {
    return matches[0]
  }

  const gateway = normalizeProfileKey($activeGatewayProfile.get())

  return matches.find(row => normalizeProfileKey(row.profile) === gateway) ?? matches[0]
}

/** Toggle the persisted unread flag: optimistic row update, then PATCH, then
 *  roll back visibly if the write fails. No-op for runtime-only sessions (a
 *  brand-new chat with no persisted row yet — there is nothing to flag). */
export async function markSessionUnread(storedId: string, unread: boolean): Promise<void> {
  const row = rowFor(storedId)

  if (!row) {
    return
  }

  // Match on (id, profile), not just id: a same-id row in another profile
  // must never be optimistically painted (or rolled back) by a toggle that
  // targeted THIS row.
  const profile = normalizeProfileKey(row.profile)
  const matchesRow = (r: (typeof row)) => r.id === storedId && normalizeProfileKey(r.profile) === profile

  const guard = new Map($unreadWriteGuard.get())
  guard.set(storedId, { at: Date.now(), profile, value: unread })
  $unreadWriteGuard.set(guard)

  setSessions(rows => rows.map(r => (matchesRow(r) ? { ...r, unread } : r)))

  try {
    await setSessionUnreadRemote(storedId, unread, row.profile)
  } catch (err) {
    // Roll back visibly: the backend kept the old value.
    const guard2 = new Map($unreadWriteGuard.get())
    guard2.delete(storedId)
    $unreadWriteGuard.set(guard2)
    setSessions(rows => rows.map(r => (matchesRow(r) ? { ...r, unread: !unread } : r)))
    throw err
  }
}

/** Opening a session clears its persisted unread flag (auto-mark-read).
 *  Best-effort: a failed PATCH is healed by the next honest refresh. */
export async function clearUnreadOnOpen(storedId: string): Promise<void> {
  const row = rowFor(storedId)

  if (!row || row.unread !== true) {
    return
  }

  try {
    await markSessionUnread(storedId, false)
  } catch {
    // Ignore: the dot simply returns until a refresh reconciles.
  }
}

/** Release guard entries once a list page confirms the value we wrote. Call
 *  once at boot, next to watchSessionPins(). */
export function watchUnreadWriteGuard(): void {
  $sessions.listen(rows => {
    const guard = $unreadWriteGuard.get()
    let changed = false

    for (const [id, entry] of guard) {
      const row = rows.find(r => r.id === id && normalizeProfileKey(r.profile) === entry.profile)

      if (row && row.unread === entry.value) {
        guard.delete(id)
        changed = true
      }
    }

    if (changed) {
      $unreadWriteGuard.set(new Map(guard))
    }
  })
}
