import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/hermes', () => ({
  getApiRequestProfile: () => null,
  speakText: vi.fn()
}))

import { speakText } from '@/hermes'

const audioResponse = { ok: true, data_url: 'data:audio/wav;base64,AAAA', mime_type: 'audio/wav' }

import { markTextSpoken, playSpeechText, wasTextAlreadySpoken } from './voice-playback'

/** jsdom lacks a real audio element — feed the playback path a controllable
 *  fake so the success branch (ended → mark spoken) runs deterministically. */
class FakeAudio {
  private listeners: Record<string, Array<() => void>> = {}
  src = ''

  addEventListener(event: string, callback: () => void): void {
    ;(this.listeners[event] ??= []).push(callback)
  }

  removeEventListener(event: string, callback: () => void): void {
    this.listeners[event] = (this.listeners[event] ?? []).filter(cb => cb !== callback)
  }

  async play(): Promise<void> {
    // Resolve, then complete like a real short clip so `ended` fires.
    setTimeout(() => this.listeners.ended?.forEach(cb => cb()), 0)
  }
}

describe('spoken-text registry', () => {
  beforeEach(() => {
    vi.stubGlobal('Audio', FakeAudio)
    vi.mocked(speakText).mockReset()
    vi.mocked(speakText).mockResolvedValue(audioResponse)
  })

  it('is false before anything has been marked spoken', () => {
    expect(wasTextAlreadySpoken('a reply nobody has spoken yet', null)).toBe(false)
  })

  it('is true for the exact text just marked spoken, ignoring whitespace reflow', () => {
    markTextSpoken('line one\n\nline two', null)

    expect(wasTextAlreadySpoken('line one line two', null)).toBe(true)
    expect(wasTextAlreadySpoken('  line one line two  ', null)).toBe(true)
  })

  it('is false for a different reply', () => {
    markTextSpoken('first reply', null)

    expect(wasTextAlreadySpoken('second reply', null)).toBe(false)
  })

  it('does not leak across sessions', () => {
    markTextSpoken('Done.', 'session-a')

    expect(wasTextAlreadySpoken('Done.', 'session-a')).toBe(true)
    expect(wasTextAlreadySpoken('Done.', 'session-b')).toBe(false)
  })

  it('marks text spoken only after playback succeeds', async () => {
    vi.mocked(speakText).mockResolvedValue(audioResponse)

    const played = await playSpeechText('hello from the test', { source: 'read-aloud' })

    expect(played).toBe(true)
    expect(wasTextAlreadySpoken('hello from the test', null)).toBe(true)
  })

  it('does not mark text spoken when playback fails before any audio is heard', async () => {
    vi.mocked(speakText).mockRejectedValueOnce(new Error('tts unavailable'))

    await expect(playSpeechText('never actually heard', { source: 'read-aloud' })).rejects.toThrow(
      'tts unavailable'
    )

    expect(wasTextAlreadySpoken('never actually heard', null)).toBe(false)
  })

  it('marks under the caller session', async () => {
    vi.mocked(speakText).mockResolvedValue(audioResponse)

    await playSpeechText('session-specific reply', { sessionId: 'session-a', source: 'read-aloud' })

    expect(wasTextAlreadySpoken('session-specific reply', 'session-a')).toBe(true)
    expect(wasTextAlreadySpoken('session-specific reply', 'session-b')).toBe(false)
  })
})