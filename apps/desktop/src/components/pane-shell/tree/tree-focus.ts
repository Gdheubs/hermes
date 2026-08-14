import { atom } from 'nanostores'

interface RestoreTreeFocusRequest {
  groupId: string
  kind: 'restore'
  paneId: string
}

export interface CloseTreeFocusRequest {
  closedPaneId: string
  groupId?: string
  id: number
  kind: 'close'
  status: 'pending' | 'settled'
}

export type TreeFocusRequest = CloseTreeFocusRequest | RestoreTreeFocusRequest

export interface TreeCloseFocusRun<T> {
  request: CloseTreeFocusRequest | null
  result: T | undefined
}

/** A keyboard interaction whose DOM target is replaced by a tree commit. */
export const $treeFocusRequest = atom<TreeFocusRequest | null>(null)

let nextCloseFocusRequestId = 0

export function requestTreeFocusAfterRestore(groupId: string, paneId: string) {
  if (hasPendingTreeCloseFocusRecovery()) {
    return
  }

  $treeFocusRequest.set({ groupId, kind: 'restore', paneId })
}

export function requestTreeFocusAfterClose(closedPaneId: string, groupId?: string): CloseTreeFocusRequest {
  const existing = $treeFocusRequest.get()

  if (existing?.kind === 'close' && existing.status === 'pending') {
    return existing
  }

  const request: CloseTreeFocusRequest = {
    closedPaneId,
    groupId,
    id: ++nextCloseFocusRequestId,
    kind: 'close',
    status: 'pending'
  }

  $treeFocusRequest.set(request)

  return request
}

/** A pending close owns the application's focus until its closer has either
 * completed or canceled. Global close commands must not replace that request
 * while a confirmation dialog (or another deferred closer) is active. */
export function hasPendingTreeCloseFocusRecovery(): boolean {
  const current = $treeFocusRequest.get()

  return current?.kind === 'close' && current.status === 'pending'
}

/** Any tree focus request owns automatic focus until the root consumes it.
 * This includes a settled close during its final recovery frame and a restore
 * request whose source rail has just been unmounted. */
export function hasTreeFocusRecovery(): boolean {
  return $treeFocusRequest.get() !== null
}

/**
 * A closer that returned synchronously, or whose deferred close settled. A
 * settled request with its source control still visible was a no-op/cancel and
 * must restore that source control if its closer displaced focus. A pending
 * request may still be awaiting a confirmation UI.
 */
export function settleTreeFocusAfterClose(request: CloseTreeFocusRequest) {
  const current = $treeFocusRequest.get()

  if (current?.kind === 'close' && current.id === request.id && current.status === 'pending') {
    $treeFocusRequest.set({ ...current, status: 'settled' })
  }
}

export function clearTreeFocusRequest(request: TreeFocusRequest) {
  const current = $treeFocusRequest.get()
  const isCurrentCloseRequest = current?.kind === 'close' && request.kind === 'close' && current.id === request.id

  if (current === request || isCurrentCloseRequest) {
    $treeFocusRequest.set(null)
  }
}

function isPromiseLike(value: unknown): value is PromiseLike<void> {
  return Boolean(value && typeof (value as PromiseLike<void>).then === 'function')
}

/**
 * Runs a tab close under the shared root-focus lifecycle. Closers may remove,
 * hide, collapse, confirm later, or reject; the root only recovers focus after
 * close outcome settles. A pending close owns the lifecycle, so a second close
 * is declined instead of overwriting the request that will recover focus.
 */
export function runTreeCloseWithFocusRecovery<T>(
  closedPaneId: string,
  close: () => T,
  groupId?: string
): TreeCloseFocusRun<T> {
  if (hasPendingTreeCloseFocusRecovery()) {
    return { request: null, result: undefined }
  }

  const request = requestTreeFocusAfterClose(closedPaneId, groupId)

  try {
    const result = close()

    if (isPromiseLike(result)) {
      void result.then(
        () => settleTreeFocusAfterClose(request),
        () => settleTreeFocusAfterClose(request)
      )
    } else {
      settleTreeFocusAfterClose(request)
    }

    return { request, result }
  } catch (error) {
    settleTreeFocusAfterClose(request)
    throw error
  }
}
