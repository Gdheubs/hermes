interface SessionPruneResult {
  removed: number
  archived?: number
  skipped_open: number
}

export function formatSessionPruneResult(result: SessionPruneResult): string {
  if (result.archived !== undefined) {
    return `Pruned ${result.removed} ended session${result.removed === 1 ? '' : 's'}; archived ${result.archived} open session${result.archived === 1 ? '' : 's'}`
  }
  const removed = `Pruned ${result.removed} session${result.removed === 1 ? '' : 's'}`
  if (!result.skipped_open) return removed

  return `${removed}. Skipped ${result.skipped_open} open session${result.skipped_open === 1 ? '' : 's'}; prune only removes ended sessions.`
}
