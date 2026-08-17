import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { $subagentsBySession, type SubagentProgress } from '@/store/subagents'

import { AgentsView } from './index'

Element.prototype.animate = function animate() {
  return {
    cancel() {},
    finished: Promise.resolve(this)
  } as unknown as Animation
}

const running = (extra: Partial<SubagentProgress> = {}): SubagentProgress => ({
  filesRead: [],
  filesWritten: [],
  goal: 'resume the interrupted Phase 7 repair',
  id: 'sa-routed',
  parentId: null,
  startedAt: 1,
  status: 'running',
  stream: [],
  taskCount: 1,
  taskIndex: 0,
  updatedAt: 1,
  ...extra
})

describe('AgentsView route display', () => {
  afterEach(() => {
    cleanup()
    $subagentsBySession.set({})
  })

  it('shows the delegation route on the far right of a subagent row', () => {
    $subagentsBySession.set({
      '20260812_130336_16394e': [running({ route: 'implement' })]
    })

    render(<AgentsView onClose={() => undefined} />)

    const row = screen.getByRole('button', { name: /resume the interrupted Phase 7 repair/i })
    const route = screen.getByTestId('subagent-route')

    expect(route.textContent).toBe('implement')
    expect(row.lastElementChild).toBe(route)
  })
})
