import { useEffect } from 'react'

import { getLatestSessionMessages, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS } from '@/hermes'
import { toChatMessages } from '@/lib/chat-messages'
import { requestGatewayForProfile } from '@/store/gateway'
import { normalizeProfileKey } from '@/store/profile'
import {
  publishSessionState,
  sessionRuntimeStateKey,
  type SessionSurfaceIdentity,
  type SessionSurfaceRuntimeIdentity,
  setSessionTileDelegate,
  StaleSessionSurfaceRuntimeError
} from '@/store/session-states'
import type { SessionResumeResponse } from '@/types/hermes'

import type { usePromptActions } from '../../session/hooks/use-prompt-actions'
import { isSessionNotFoundError, markSessionRecentlyInterrupted, withSessionNotFoundResume } from '../../session/hooks/use-prompt-actions/utils'
import { resolveSessionProfile } from '../../session/hooks/use-session-actions/utils'
import type { useSessionStateCache } from '../../session/hooks/use-session-state-cache'
import type { GatewayRequester } from '../types'

type SessionStateCache = ReturnType<typeof useSessionStateCache>

interface SessionTileDelegateParams {
  archiveSession: (storedSessionId: string) => Promise<unknown>
  branchStoredSession: (storedSessionId: string) => Promise<unknown>
  executeSlashCommand: ReturnType<typeof usePromptActions>['executeSlashCommand']
  removeSession: (storedSessionId: string) => Promise<unknown>
  requestGateway: GatewayRequester
  runtimeIdByStoredSessionIdRef: SessionStateCache['runtimeIdByStoredSessionIdRef']
  sessionStateByRuntimeIdRef: SessionStateCache['sessionStateByRuntimeIdRef']
  updateSessionState: SessionStateCache['updateSessionState']
}

/**
 * Publishes the session-tile delegate: resume / adopt / submit / interrupt /
 * slash for tiled AND embedded surfaces WITHOUT touching the primary view
 * ($activeSessionId / $messages stay the main thread's). Surface resume and
 * adopt route through the identity's OWNING profile gateway
 * (requestGatewayForProfile) so a background-profile surface never detours the
 * foreground; a cold surface binds + hydrates the cache, which
 * publishSessionState mirrors to the surface.
 */
export function useSessionTileDelegate({
  archiveSession,
  branchStoredSession,
  executeSlashCommand,
  removeSession,
  requestGateway,
  runtimeIdByStoredSessionIdRef,
  sessionStateByRuntimeIdRef,
  updateSessionState
}: SessionTileDelegateParams): void {
  useEffect(() => {
    // Durable surface bindings keyed by profile-qualified stored identity. A
    // runtime id is a cache warm-path hint, never the identity itself.
    const surfaceRuntimeByIdentity = new Map<string, string>()

    const surfaceKey = ({ profile, storedSessionId }: SessionSurfaceIdentity) =>
      `${normalizeProfileKey(profile)}\u0000${storedSessionId}`

    const discardSurface = ({ profile, storedSessionId }: SessionSurfaceIdentity): string[] => {
      const ownerProfile = normalizeProfileKey(profile)
      const identityKey = surfaceKey({ profile: ownerProfile, storedSessionId })
      const runtimeId = surfaceRuntimeByIdentity.get(identityKey)

      surfaceRuntimeByIdentity.delete(identityKey)
      runtimeIdByStoredSessionIdRef.current.delete(storedSessionId)

      if (runtimeId) {
        sessionStateByRuntimeIdRef.current.delete(sessionRuntimeStateKey(ownerProfile, runtimeId))
      }

      return runtimeId ? [runtimeId] : []
    }

    const discardStaleSurfaceRuntime = (identity: SessionSurfaceRuntimeIdentity) => {
      const key = surfaceKey(identity)

      if (surfaceRuntimeByIdentity.get(key) === identity.runtimeSessionId) {
        surfaceRuntimeByIdentity.delete(key)
      }
    }

    // A tile's runtime binding can die the same way the foreground's does
    // (sleep/wake, backend restart). The cache maps stored -> runtime, so walk
    // it backwards to find the durable id this runtime belongs to.
    const storedSessionIdForRuntime = (runtimeId: string): null | string => {
      const cached = sessionStateByRuntimeIdRef.current.get(runtimeId)?.storedSessionId

      if (cached) {
        return cached
      }

      for (const [storedId, mapped] of runtimeIdByStoredSessionIdRef.current) {
        if (mapped === runtimeId) {
          return storedId
        }
      }

      return null
    }

    // Repoint the stored -> runtime mapping at the recovered id so subsequent
    // tile actions use the live binding instead of re-recovering every call.
    const rebindTileRuntime = (deadRuntimeId: string) => (recoveredId: string) => {
      const storedId = storedSessionIdForRuntime(deadRuntimeId)

      if (storedId) {
        runtimeIdByStoredSessionIdRef.current.set(storedId, recoveredId)
      }
    }

    const resumeSurface = async ({ profile, storedSessionId }: SessionSurfaceIdentity) => {
      if (!profile.trim()) {
        throw new Error('SessionSurface requires an explicit profile')
      }

      const surfaceRuntime = surfaceRuntimeByIdentity.get(surfaceKey({ profile, storedSessionId }))

      const cached = surfaceRuntime
        ? sessionStateByRuntimeIdRef.current.get(sessionRuntimeStateKey(profile, surfaceRuntime))
        : undefined

      if (surfaceRuntime && cached?.storedSessionId === storedSessionId) {
        publishSessionState(surfaceRuntime, cached)

        return surfaceRuntime
      }

      const [prefetch, resumed] = await Promise.all([
        getLatestSessionMessages(storedSessionId, profile).catch(() => null),
        requestGatewayForProfile<SessionResumeResponse>(profile, 'session.resume', {
          session_id: storedSessionId,
          cols: 96,
          omit_messages: true,
          profile
        })
      ])

      const runtimeId = resumed?.session_id

      if (!runtimeId) {
        throw new Error('resume returned no session id')
      }

      surfaceRuntimeByIdentity.set(surfaceKey({ profile, storedSessionId }), runtimeId)
      updateSessionState(
        runtimeId,
        state => ({
          ...state,
          busy: Boolean(resumed?.info?.running),
          messages: state.messages.length > 0 ? state.messages : toChatMessages(prefetch?.messages ?? resumed?.messages ?? [])
        }),
        storedSessionId
      )

      return runtimeId
    }

    setSessionTileDelegate({
      adoptSurface: async (identity: SessionSurfaceRuntimeIdentity) => {
        if (!identity.profile.trim()) {
          throw new Error('SessionSurface requires an explicit profile')
        }

        let status: { output?: string }

        try {
          status = await requestGatewayForProfile<{ output?: string }>(identity.profile, 'session.status', {
            session_id: identity.runtimeSessionId
          })
        } catch (error) {
          if (isSessionNotFoundError(error)) {
            discardStaleSurfaceRuntime(identity)
            throw new StaleSessionSurfaceRuntimeError('Session surface runtime hint was not found')
          }

          throw error
        }

        const authoritativeStoredId = status.output
          ?.split('\n')
          .find(line => line.startsWith('Session ID: '))
          ?.slice('Session ID: '.length)
          .trim()

        if (authoritativeStoredId !== identity.storedSessionId) {
          discardStaleSurfaceRuntime(identity)
          throw new StaleSessionSurfaceRuntimeError('Session surface identity mismatch')
        }

        surfaceRuntimeByIdentity.set(surfaceKey(identity), identity.runtimeSessionId)

        const cached = sessionStateByRuntimeIdRef.current.get(
          sessionRuntimeStateKey(identity.profile, identity.runtimeSessionId)
        )

        updateSessionState(
          identity.runtimeSessionId,
          state =>
            cached?.storedSessionId === identity.storedSessionId
              ? { ...state }
              : { ...state, messages: [], streamId: null, busy: false, awaitingResponse: false },
          identity.storedSessionId
        )

        return identity.runtimeSessionId
      },
      archiveSession: async (storedSessionId, profile) => {
        await archiveSession(storedSessionId)

        if (profile) {
          discardSurface({ profile, storedSessionId })
        }
      },
      branchSession: async (storedSessionId) => {
        await branchStoredSession(storedSessionId)
      },
      deleteSession: async (storedSessionId, profile) => {
        await removeSession(storedSessionId)

        if (profile) {
          discardSurface({ profile, storedSessionId })
        }
      },
      executeSlash: async (rawCommand, sessionId) => {
        await executeSlashCommand(rawCommand, { sessionId })
      },
      // Gateway reconnect (sleep/wake, backend respawn): every stored→runtime
      // binding recorded pre-reconnect points at a runtime id the respawned
      // backend no longer knows. Drop the map so resumeTile's warm path can't
      // re-bind a tile to a dead runtime; live bindings re-record from
      // post-reconnect events and fresh resumes.
      invalidateRuntimeBindings: () => {
        runtimeIdByStoredSessionIdRef.current.clear()
      },
      interruptSession: async runtimeId => {
        // Same cooldown as the primary chat's Stop (#83855): the gateway may
        // still be winding down after this interrupt, so a quick edit/resend
        // on the tile must go interrupt-first even though busy already reads
        // false. Mark the runtime id (and any recovered id) before the RPC so
        // the window covers the whole wind-down.
        markSessionRecentlyInterrupted(runtimeId)
        await withSessionNotFoundResume(
          runtimeId,
          storedSessionIdForRuntime(runtimeId),
          liveId => requestGateway('session.interrupt', { session_id: liveId }),
          {
            requestGateway,
            onRecovered: recoveredId => {
              markSessionRecentlyInterrupted(recoveredId)
              rebindTileRuntime(runtimeId)(recoveredId)
            }
          }
        )
      },
      resumeSurface,
      resumeTile: async storedSessionId => {
        const existing = runtimeIdByStoredSessionIdRef.current.get(storedSessionId)
        const cached = existing ? sessionStateByRuntimeIdRef.current.get(existing) : undefined

        // Warm path: reuse a live binding — but only when it still carries a
        // transcript (or is mid-turn, where messages legitimately stream in).
        // A binding whose cached state has no messages is either a released
        // transcript or a stale pre-reconnect survivor; reusing it painted the
        // post-sleep/wake tile permanently empty. Fall through to a real
        // resume instead — it's idempotent for a genuinely live session.
        if (existing && cached?.storedSessionId === storedSessionId && (cached.busy || cached.messages.length > 0)) {
          publishSessionState(existing, cached)

          return existing
        }

        // Resolve the owning profile before binding a runtime. A tile can open a
        // session from any profile, not just the active one; resuming (or
        // reading messages) without a profile lets the gateway fall back to the
        // launch-profile DB and fork the conversation into the wrong profile —
        // the same cross-profile bleed the recovery resumes had (#67603).
        const profile = await resolveSessionProfile(storedSessionId)

        if (!profile) {
          throw new Error('Session surface profile unavailable')
        }

        const [prefetch, resumed] = await Promise.all([
          getLatestSessionMessages(storedSessionId, profile).catch(() => null),
          requestGateway<SessionResumeResponse>('session.resume', {
            session_id: storedSessionId,
            cols: 96,
            omit_messages: true,
            profile
          })
        ])

        const runtimeId = resumed?.session_id

        if (!runtimeId) {
          throw new Error('resume returned no session id')
        }

        updateSessionState(
          runtimeId,
          state => ({
            ...state,
            busy: Boolean(resumed?.info?.running),
            messages: state.messages.length > 0 ? state.messages : toChatMessages(prefetch?.messages ?? resumed?.messages ?? [])
          }),
          storedSessionId
        )

        return runtimeId
      },
      submitToSession: async (runtimeId, text) => {
        await withSessionNotFoundResume(
          runtimeId,
          storedSessionIdForRuntime(runtimeId),
          liveId => requestGateway('prompt.submit', { session_id: liveId, text }, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS),
          { requestGateway, onRecovered: rebindTileRuntime(runtimeId) }
        )
      },
      updateSession: (runtimeId, updater) => updateSessionState(runtimeId, updater)
    })
  }, [
    archiveSession,
    branchStoredSession,
    executeSlashCommand,
    removeSession,
    requestGateway,
    runtimeIdByStoredSessionIdRef,
    sessionStateByRuntimeIdRef,
    updateSessionState
  ])
}
