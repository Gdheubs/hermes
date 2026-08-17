/**
 * Spoken-reply identity for TTS dedupe (auto-speak / voice conversation).
 *
 * The reply row's `id` is NOT stable across a turn: while streaming it is a
 * renderer id (`assistant-stream-<session>`), and committing the turn rewrites
 * the row under the durable backend id — same reply, new handle. Keying the
 * "already spoken" check on `id` alone makes the rewritten row look unspoken,
 * so the reply is read aloud a second time at the playback-idle edge (the
 * auto-speak "plays twice" report). The dedupe anchors on normalized text so
 * the rewrite is absorbed, and migrates the id anchor so later reads hit the
 * fast path.
 */

const normalizeSpokenText = (text: string) => text.replace(/\s+/g, ' ').trim()

export interface SpokenReplyDedupe {
  /** True when this reply was already spoken. If the SAME reply now carries a
   *  new id (end-of-turn rewrite), re-anchors to the new id and returns true. */
  isSpoken: (id: string, text: string) => boolean
  /** Record this reply as spoken, anchoring on the current id + text. */
  markSpoken: (id: string, text: string) => void
  /** The id of the last spoken reply (migrated across rewrites) — used by
   *  id-based selectors (voice-conversation turn speech). */
  lastId: () => string | null
}

export function createSpokenReplyDedupe(): SpokenReplyDedupe {
  let lastId: string | null = null
  let lastText: string | null = null

  const isSpoken = (id: string, text: string): boolean => {
    if (id === lastId) {
      return true
    }

    // Same text, different id → the committed rewrite of the reply we just
    // spoke. Absorb the new id so the conversation path's id-based lookups
    // stay consistent, and report it as spoken.
    if (lastText !== null && normalizeSpokenText(text) === lastText) {
      lastId = id

      return true
    }

    return false
  }

  const markSpoken = (id: string, text: string): void => {
    lastId = id
    lastText = normalizeSpokenText(text)
  }

  return { isSpoken, markSpoken, lastId: () => lastId }
}