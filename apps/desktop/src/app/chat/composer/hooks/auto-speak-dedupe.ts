/**
 * Dedupes auto-speak / voice-conversation "already spoken" tracking.
 *
 * Desktop often re-emits the same completed assistant bubble under a new id
 * when stream → final hydration lands. Id-only tracking then re-fires
 * `/api/audio/speak-stream` after the first clip goes idle (~seconds later).
 */

export type SpokenReplyBook = {
  ids: Set<string>
  /** Full speakable text of the last completed reply we marked spoken. */
  text: string | null
}

export function createSpokenReplyBook(): SpokenReplyBook {
  return { ids: new Set(), text: null }
}

export function clearSpokenReplyBook(book: SpokenReplyBook): void {
  book.ids.clear()
  book.text = null
}

export function markAssistantReplySpoken(
  book: SpokenReplyBook,
  reply: { id: string; text?: string | null }
): void {
  book.ids.add(reply.id)
  const text = reply.text?.trim()
  if (text) {
    book.text = text
  }
}

/**
 * True when this assistant bubble should not be spoken again.
 * - Same message id already spoken, or
 * - Completed bubble whose text matches the last spoken completed text
 *   (stream id replaced by final id with identical body).
 */
export function isAssistantReplyAlreadySpoken(
  book: SpokenReplyBook,
  reply: { id: string; pending?: boolean; text: string }
): boolean {
  if (book.ids.has(reply.id)) {
    return true
  }

  const text = reply.text.trim()
  if (!text) {
    return false
  }

  if (!reply.pending && book.text !== null && text === book.text) {
    return true
  }

  return false
}
