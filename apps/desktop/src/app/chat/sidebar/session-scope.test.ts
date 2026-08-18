import { describe, expect, it } from 'vitest'

import { ALL_PROFILES } from '@/store/profile'
import type { SessionInfo } from '@/types/hermes'

import { selectVisibleSessions } from './session-scope'

function session(id: string, profile: string | null = 'default'): SessionInfo {
  return {
    id,
    profile: profile ?? undefined,
    title: id,
    ended_at: null,
    input_tokens: 0,
    is_active: false,
    last_active: 0,
    message_count: 0,
    model: null,
    output_tokens: 0,
    preview: null,
    source: null,
    started_at: 0,
    tool_call_count: 0
  }
}

describe('selectVisibleSessions', () => {
  it('returns every session for the ALL_PROFILES scope', () => {
    const sessions = [session('a', 'default'), session('b', 'work'), session('c', 'default')]
    const result = selectVisibleSessions(sessions, ALL_PROFILES)

    expect(result).toHaveLength(3)
    expect(result.map(s => s.id)).toEqual(['a', 'b', 'c'])
  })

  it('keeps ALL sessions even when the scope sentinel is ALL and a single profile owns them', () => {
    // Regression for the sidebar bug: a single-profile user who lands in the
    // ALL scope (Grouping → Profile persists $showAllProfiles) must still see
    // every row — the scope selector must not filter against the `__all__`
    // sentinel, which matches nothing.
    const sessions = [session('a', 'default'), session('b', 'default'), session('c', 'default')]
    const result = selectVisibleSessions(sessions, ALL_PROFILES)

    expect(result).toHaveLength(3)
  })

  it('returns only the matching profile for a concrete scope', () => {
    const sessions = [session('a', 'default'), session('b', 'work'), session('c', 'default')]
    const result = selectVisibleSessions(sessions, 'default')

    expect(result.map(s => s.id)).toEqual(['a', 'c'])
  })

  it('normalizes the session profile before matching a concrete scope', () => {
    const sessions = [session('a', 'default'), session('b', '  work  ')]
    const result = selectVisibleSessions(sessions, 'work')

    expect(result.map(s => s.id)).toEqual(['b'])
  })

  it('treats a null profile as default', () => {
    const sessions = [session('a', null), session('b', 'work')]
    const result = selectVisibleSessions(sessions, 'default')

    expect(result.map(s => s.id)).toEqual(['a'])
  })

  it('returns an empty array when no session matches a concrete scope', () => {
    const sessions = [session('a', 'default'), session('b', 'work')]
    const result = selectVisibleSessions(sessions, 'other')

    expect(result).toEqual([])
  })
})
