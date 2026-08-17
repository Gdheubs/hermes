import { describe, expect, it } from 'vitest'

import {
  clearSpokenReplyBook,
  createSpokenReplyBook,
  isAssistantReplyAlreadySpoken,
  markAssistantReplySpoken
} from './auto-speak-dedupe'

describe('auto-speak-dedupe', () => {
  it('marks and matches by message id', () => {
    const book = createSpokenReplyBook()
    markAssistantReplySpoken(book, { id: 'stream-1', text: 'hello' })
    expect(
      isAssistantReplyAlreadySpoken(book, { id: 'stream-1', pending: false, text: 'hello' })
    ).toBe(true)
    expect(
      isAssistantReplyAlreadySpoken(book, { id: 'other', pending: false, text: 'different' })
    ).toBe(false)
  })

  it('treats completed same-text under a new id as already spoken (stream→final)', () => {
    const book = createSpokenReplyBook()
    markAssistantReplySpoken(book, { id: 'stream-temp', text: 'Hello world' })

    // Final hydration replaces the id; body unchanged.
    expect(
      isAssistantReplyAlreadySpoken(book, {
        id: 'final-permanent',
        pending: false,
        text: 'Hello world'
      })
    ).toBe(true)
  })

  it('does not block a later distinct reply with different text', () => {
    const book = createSpokenReplyBook()
    markAssistantReplySpoken(book, { id: 'a', text: 'first answer' })

    expect(
      isAssistantReplyAlreadySpoken(book, {
        id: 'b',
        pending: false,
        text: 'second answer'
      })
    ).toBe(false)
  })

  it('does not text-dedupe while the bubble is still pending/streaming', () => {
    const book = createSpokenReplyBook()
    markAssistantReplySpoken(book, { id: 'old', text: 'partial' })

    // Streaming growth can share a prefix with a prior mark if mis-ordered;
    // only completed bubbles use text equality.
    expect(
      isAssistantReplyAlreadySpoken(book, {
        id: 'stream-2',
        pending: true,
        text: 'partial'
      })
    ).toBe(false)
  })

  it('clearSpokenReplyBook resets id and text memory (session switch)', () => {
    const book = createSpokenReplyBook()
    markAssistantReplySpoken(book, { id: 'a', text: 'hi' })
    clearSpokenReplyBook(book)
    expect(
      isAssistantReplyAlreadySpoken(book, { id: 'a', pending: false, text: 'hi' })
    ).toBe(false)
  })
})
