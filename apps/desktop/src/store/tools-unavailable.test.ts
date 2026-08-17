import { afterEach, describe, expect, it } from 'vitest'

import { $toolsUnavailable, setToolsUnavailable } from '@/store/tools-unavailable'

describe('$toolsUnavailable', () => {
  afterEach(() => {
    $toolsUnavailable.set({})
  })

  it('stores the unavailable tool names per session id', () => {
    setToolsUnavailable('session-a', ['browser_navigate', 'web_search'])

    expect($toolsUnavailable.get()).toEqual({ 'session-a': ['browser_navigate', 'web_search'] })
  })

  it('keeps distinct sessions separate', () => {
    setToolsUnavailable('session-a', ['browser_navigate'])
    setToolsUnavailable('session-b', ['tts_speak'])

    expect($toolsUnavailable.get()).toEqual({
      'session-a': ['browser_navigate'],
      'session-b': ['tts_speak']
    })
  })

  it('ignores calls without a session id', () => {
    setToolsUnavailable('', ['browser_navigate'])

    expect($toolsUnavailable.get()).toEqual({})
  })

  it('bounds the map to the most recent sessions, dropping the oldest first', () => {
    // MAX_TOOLS_UNAVAILABLE_ENTRIES is 100; fill past it and verify the
    // oldest entries fall off (string-key insertion order = LRU-ish).
    for (let i = 0; i < 105; i += 1) {
      setToolsUnavailable(`session-${String(i).padStart(3, '0')}`, ['browser_navigate'])
    }

    const keys = Object.keys($toolsUnavailable.get())
    expect(keys).toHaveLength(100)
    expect(keys[0]).toBe('session-005')
    expect(keys[99]).toBe('session-104')
  })
})
