import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $toolsUnavailable } from '@/store/tools-unavailable'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const SID = 'tools-unavailable-session'

// Module-scoped so a test can seed session state before the handler reads it.
const sessionStates = new Map<string, ClientSessionState>()
let handleEvent: ((event: RpcEvent) => void) | null = null

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  const sessionStateByRuntimeIdRef = useRef(sessionStates)
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      const next = updater(sessionStates.get(sessionId) ?? createClientSessionState())
      sessionStates.set(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

async function mountStream() {
  render(<Harness />)
  await waitFor(() => expect(handleEvent).not.toBeNull())
}

function emit(type: RpcEvent['type'], payload: RpcEvent['payload'] = {}, sessionId = SID) {
  act(() => handleEvent!({ payload, session_id: sessionId, type }))
}

describe('tools.unavailable wiring', () => {
  beforeEach(() => {
    handleEvent = null
    sessionStates.clear()
    $toolsUnavailable.set({})
  })

  afterEach(() => {
    cleanup()
    sessionStates.clear()
    $toolsUnavailable.set({})
    vi.restoreAllMocks()
  })

  it('stores the unavailable tool names for the session', async () => {
    await mountStream()

    emit('tools.unavailable', { names: ['browser_navigate', 'web_search', 'tts_speak'] })

    expect($toolsUnavailable.get()[SID]).toEqual(['browser_navigate', 'web_search', 'tts_speak'])
  })

  it('ignores events without a names array', async () => {
    await mountStream()

    emit('tools.unavailable', {})
    emit('tools.unavailable', { names: 'not-an-array' } as unknown as RpcEvent['payload'])

    expect($toolsUnavailable.get()[SID]).toBeUndefined()
  })
})
