import { describe, expect, it } from 'vitest'

import type { KanbanColumn } from './types'
import { partitionSwimLanes } from './swim-lanes'

const columns: KanbanColumn[] = [
  {
    name: 'todo',
    tasks: [
      { id: 'org-1', title: 'Org card', status: 'todo', assignee: 'R1ft' },
      { id: 'w3bb-1', title: 'W3bb card', status: 'todo', assignee: 'w3bb' }
    ]
  },
  {
    name: 'ready',
    tasks: [
      { id: 'l3x-1', title: 'L3x card', status: 'ready', assignee: 'l3x' },
      { id: 'unassigned-1', title: 'Unassigned card', status: 'ready', assignee: null }
    ]
  }
]

describe('partitionSwimLanes', () => {
  it('keeps W3bb cards out of Org while preserving L3x and unassigned bucketing', () => {
    const lanes = partitionSwimLanes(columns, 'l3x', 'w3bb', true)

    expect(lanes.map(lane => lane.id)).toEqual(['__other__', 'w3bb', 'l3x'])
    expect(lanes.map(lane => lane.label)).toEqual(['Org', 'W3bb', 'l3x'])
    expect(lanes.map(lane => lane.task_count)).toEqual([2, 1, 1])
    expect(lanes[0].columns.flatMap(column => column.tasks).map(task => task.id)).toEqual(['org-1', 'unassigned-1'])
    expect(lanes[1].columns.flatMap(column => column.tasks).map(task => task.id)).toEqual(['w3bb-1'])
    expect(lanes[2].columns.flatMap(column => column.tasks).map(task => task.id)).toEqual(['l3x-1'])
  })

  it('folds W3bb back into Org when its dedicated lane is disabled', () => {
    const lanes = partitionSwimLanes(columns, 'l3x', 'w3bb', false)

    expect(lanes.map(lane => lane.id)).toEqual(['__other__', 'l3x'])
    expect(lanes[0].columns.flatMap(column => column.tasks).map(task => task.id)).toEqual([
      'org-1',
      'w3bb-1',
      'unassigned-1'
    ])
  })
})
