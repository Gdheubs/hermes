import { atom } from 'nanostores'

// Config-gated tools (browser, vision, tts, ...) that failed their check_fn
// for this session (issue #1433 Phase 2). The gateway emits `tools.unavailable`
// once per session build; the thread renders the notice in the fresh-session
// empty state. Keyed by session id; a stale entry is inert because it only
// renders while that session's thread is mounted.
//
// Bounded to the most recent sessions: entries are inert once their thread is
// unmounted, so only sessions the user may still open fresh need to survive.
// JS object string-key insertion order makes dropping the oldest entries an
// LRU-ish trim.
const MAX_TOOLS_UNAVAILABLE_ENTRIES = 100
const $toolsUnavailable = atom<Record<string, string[]>>({})

export { $toolsUnavailable }

export function setToolsUnavailable(sessionId: string, names: string[]) {
  if (!sessionId) {
    return
  }

  const current = $toolsUnavailable.get()
  const next = { ...current, [sessionId]: names }
  const keys = Object.keys(next)

  if (keys.length > MAX_TOOLS_UNAVAILABLE_ENTRIES) {
    for (const stale of keys.slice(0, keys.length - MAX_TOOLS_UNAVAILABLE_ENTRIES)) {
      delete next[stale]
    }
  }

  $toolsUnavailable.set(next)
}
