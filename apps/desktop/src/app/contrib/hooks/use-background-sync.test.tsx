import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $changeEventsAvailable, $cronChangeTick, $sessionsChangeTick } from '@/store/live-sync'
import { $activeSessionId, $unreadFinishedSessionIds } from '@/store/session'
import { $attentionSessionIds, clearAllSessionStates } from '@/store/session-states'

import { rehydrateLiveSessionStatuses, useBackgroundSync } from './use-background-sync'

const noop = () => undefined
const requestGateway = async () => ({ sessions: [] })

function render(activeGatewayProfile: string, refreshSessions: () => Promise<void>) {
  return renderHook(
    ({ profile }: { profile: string }) => {
      useBackgroundSync({
        activeGatewayProfile: profile,
        activeIsMessaging: false,
        activeSessionId: null,
        freshDraftReady: false,
        gatewayState: 'open',
        refreshActiveMessagingTranscript: noop,
        refreshCronJobs: noop,
        refreshCurrentModel: noop,
        refreshHermesConfig: noop,
        refreshMessagingSessions: noop,
        refreshSessions,
        requestGateway
      })
    },
    { initialProps: { profile: activeGatewayProfile } }
  )
}

describe('useBackgroundSync profile-scoped session refresh', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    $activeSessionId.set(null)
    $changeEventsAvailable.set(false)
    $cronChangeTick.set(0)
    $sessionsChangeTick.set(0)
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('refreshes the session list after the active gateway profile changes', async () => {
    const refreshSessions = vi.fn(async () => undefined)
    const hook = render('default', refreshSessions)

    await act(async () => undefined)
    expect(refreshSessions).toHaveBeenCalledTimes(1)
    refreshSessions.mockClear()

    hook.rerender({ profile: 'nova' })

    await act(async () => undefined)
    expect(refreshSessions).toHaveBeenCalledTimes(1)
  })
})

describe('rehydrateLiveSessionStatuses approval-blocked status mapping', () => {
  beforeEach(() => {
    clearAllSessionStates()
    $unreadFinishedSessionIds.set([])
    $activeSessionId.set(null)
  })

  afterEach(() => {
    clearAllSessionStates()
    $unreadFinishedSessionIds.set([])
    $activeSessionId.set(null)
  })

  it('maps status "waiting" to needsInput so the amber dot survives the poll', () => {
    rehydrateLiveSessionStatuses({
      sessions: [{ id: 'rt-waiting', session_key: 'stored-waiting', status: 'waiting' }]
    })

    expect($attentionSessionIds.get()).toContain('stored-waiting')
  })

  it('does not flag a "working" session as needing input', () => {
    rehydrateLiveSessionStatuses({
      sessions: [{ id: 'rt-working', session_key: 'stored-working', status: 'working' }]
    })

    expect($attentionSessionIds.get()).not.toContain('stored-working')
  })
})
