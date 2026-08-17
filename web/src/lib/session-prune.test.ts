import { describe, expect, it } from 'vitest'

import { formatSessionPruneResult } from './session-prune'

describe('formatSessionPruneResult', () => {
  it('reports open sessions skipped by the prune safety guard', () => {
    expect(formatSessionPruneResult({ removed: 0, skipped_open: 2 })).toBe(
      'Pruned 0 sessions. Skipped 2 open sessions; prune only removes ended sessions.'
    )
  })

  it('keeps the existing success message when nothing was skipped', () => {
    expect(formatSessionPruneResult({ removed: 1, skipped_open: 0 })).toBe('Pruned 1 session')
  })

  it('reports archived open sessions separately from deleted ended sessions', () => {
    expect(formatSessionPruneResult({ removed: 2, archived: 3, skipped_open: 0 })).toBe(
      'Pruned 2 ended sessions; archived 3 open sessions'
    )
  })

  it('keeps the separate archive count when no open sessions matched', () => {
    expect(formatSessionPruneResult({ removed: 1, archived: 0, skipped_open: 0 })).toBe(
      'Pruned 1 ended session; archived 0 open sessions'
    )
  })
})
