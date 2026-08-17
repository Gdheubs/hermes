import { describe, expect, it } from 'vitest'

import { createSpokenReplyDedupe } from './spoken-reply'

describe('createSpokenReplyDedupe', () => {
  it('treats the same id as spoken', () => {
    const dedupe = createSpokenReplyDedupe()
    dedupe.markSpoken('assistant-stream-s', 'the answer')

    expect(dedupe.isSpoken('assistant-stream-s', 'the answer')).toBe(true)
  })

  it('absorbs the end-of-turn id rewrite: same text, new durable id', () => {
    const dedupe = createSpokenReplyDedupe()
    dedupe.markSpoken('assistant-stream-s', 'the answer')

    // End of turn: the renderer stream row is rewritten under the durable
    // backend id, carrying the identical reply text.
    expect(dedupe.isSpoken('42', 'the answer')).toBe(true)
    // The anchor migrated: a later read hits the id fast path.
    expect(dedupe.isSpoken('42', 'the answer')).toBe(true)
    expect(dedupe.lastId()).toBe('42')
  })

  it('compares text whitespace-insensitively across the rewrite', () => {
    const dedupe = createSpokenReplyDedupe()
    dedupe.markSpoken('assistant-stream-s', 'line one\n\nline two')

    expect(dedupe.isSpoken('42', 'line one line two')).toBe(true)
  })

  it('speaks a genuinely new reply with different text', () => {
    const dedupe = createSpokenReplyDedupe()
    dedupe.markSpoken('assistant-stream-s', 'first answer')

    expect(dedupe.isSpoken('assistant-stream-s', 'first answer')).toBe(true)
    expect(dedupe.isSpoken('43', 'second answer')).toBe(false)
  })

  it('stays silent before anything was spoken', () => {
    const dedupe = createSpokenReplyDedupe()

    expect(dedupe.isSpoken('any-id', 'any text')).toBe(false)
    expect(dedupe.lastId()).toBeNull()
  })
})