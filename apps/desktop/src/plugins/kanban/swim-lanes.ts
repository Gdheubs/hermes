import type { KanbanColumn, KanbanSwimLane } from './types'

export const SWIM_LANE_ORG_ID = '__other__'
const W3BB_SWIM_LANE_LABEL = 'W3bb'

/**
 * Split the flat board into the cross-column profile rows used by both the
 * dashboard and the desktop board. Cards remain in their original column and
 * order; only their lane membership changes.
 */
export function partitionSwimLanes(
  columns: KanbanColumn[],
  l3xAssignee: string,
  w3bbAssignee: string,
  w3bbSwimLane: boolean
): KanbanSwimLane[] {
  const dedicatedAssignees = new Set([l3xAssignee])

  if (w3bbSwimLane) {
    dedicatedAssignees.add(w3bbAssignee)
  }

  const l3xCols: KanbanColumn[] = []
  const w3bbCols: KanbanColumn[] = []
  const otherCols: KanbanColumn[] = []

  for (const col of columns) {
    const l3xTasks = col.tasks.filter(task => task.assignee === l3xAssignee)
    const w3bbTasks = w3bbSwimLane ? col.tasks.filter(task => task.assignee === w3bbAssignee) : []
    const otherTasks = col.tasks.filter(task => {
      const assignee = task.assignee
      return assignee == null || !dedicatedAssignees.has(assignee)
    })

    l3xCols.push({ name: col.name, tasks: l3xTasks })
    w3bbCols.push({ name: col.name, tasks: w3bbTasks })
    otherCols.push({ name: col.name, tasks: otherTasks })
  }

  const count = (cols: KanbanColumn[]) => cols.reduce((sum, col) => sum + col.tasks.length, 0)
  const lanes: KanbanSwimLane[] = [
    {
      id: SWIM_LANE_ORG_ID,
      label: 'Org',
      assignee: null,
      task_count: count(otherCols),
      columns: otherCols
    }
  ]

  if (w3bbSwimLane) {
    lanes.push({
      id: w3bbAssignee,
      label: W3BB_SWIM_LANE_LABEL,
      assignee: w3bbAssignee,
      task_count: count(w3bbCols),
      columns: w3bbCols
    })
  }

  lanes.push({
    id: l3xAssignee,
    label: l3xAssignee,
    assignee: l3xAssignee,
    task_count: count(l3xCols),
    columns: l3xCols
  })

  return lanes
}
