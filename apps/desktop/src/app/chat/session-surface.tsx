import { useEffect, useRef, useState } from 'react'

import { CenteredThreadSpinner } from '@/components/assistant-ui/thread/status'
import { profileGatewayState, subscribeProfileGateway } from '@/store/gateway'
import type { SessionSurfaceIdentity } from '@/store/session-states'
import {
  bindSessionSurfaceRuntime,
  isStaleSessionSurfaceRuntimeError,
  releaseSessionSurfaceReference,
  retainSessionSurfaceReference,
  sessionSurfaceDelegate,
  subscribeSessionSurfaceDelegate
} from '@/store/session-states'

import { SessionSurfaceChat } from './session-surface-chat'

export type { SessionSurfaceIdentity } from '@/store/session-states'

export interface SessionSurfaceProps {
  session: SessionSurfaceIdentity
}

export interface SessionSurfaceCoreProps extends SessionSurfaceIdentity {
  /** Core-only lifecycle notification used by session tiles. */
  onRuntimeSessionId?: (runtimeSessionId: string) => void
}

function validIdentityPart(value: unknown): value is string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    return false
  }

  for (const character of value) {
    const code = character.codePointAt(0)

    if (code === undefined || code < 32 || code === 127) {
      return false
    }
  }

  return true
}

export function SessionSurfaceCore({
  onRuntimeSessionId,
  profile,
  runtimeSessionId,
  storedSessionId
}: SessionSurfaceCoreProps) {
  const valid =
    validIdentityPart(profile) &&
    validIdentityPart(storedSessionId) &&
    (runtimeSessionId == null || validIdentityPart(runtimeSessionId))

  const identityKey = valid ? `${profile}\u0000${storedSessionId}` : ''
  const [binding, setBinding] = useState<{ error?: boolean; identityKey?: string; runtimeSessionId?: string }>({})
  const [readinessGeneration, setReadinessGeneration] = useState(0)
  const [retryGeneration, setRetryGeneration] = useState(0)
  const mountedRef = useRef(false)
  const identityGenerationRef = useRef({ identityKey: '', value: 0 })

  const attemptRef = useRef<{
    generation: number
    retryGeneration: number
    status: 'failed' | 'in-flight' | 'succeeded'
    transportGeneration: number
  } | null>(null)

  const connectionLostRef = useRef(false)
  const staleRuntimeHintRef = useRef('')
  const transportGenerationRef = useRef(0)
  const bindingIdentityKey = valid ? `${identityKey}\u0000${runtimeSessionId ?? ''}` : ''

  if (identityGenerationRef.current.identityKey !== bindingIdentityKey) {
    identityGenerationRef.current = {
      identityKey: bindingIdentityKey,
      value: identityGenerationRef.current.value + 1
    }
    connectionLostRef.current = false
    staleRuntimeHintRef.current = ''
  }

  // Lifecycle token, not a reactive-value mirror: async bindings must ignore unmount completion.
  // eslint-disable-next-line no-restricted-syntax
  useEffect(() => {
    mountedRef.current = true

    return () => {
      mountedRef.current = false
    }
  }, [])

  // Binding coordination is intentionally non-rendering hot state, not a mirrored atom.
  // eslint-disable-next-line no-restricted-syntax
  useEffect(() => {
    const delegateChanged = () => {
      if (attemptRef.current?.status !== 'in-flight') {
        attemptRef.current = null
        setReadinessGeneration(value => value + 1)
      }
    }

    const gatewayChanged = () => {
      const open = profileGatewayState(profile)?.connectionState === 'open'

      if (!open) {
        if (!connectionLostRef.current) {
          transportGenerationRef.current += 1
        }

        connectionLostRef.current = true

        return
      }

      if (connectionLostRef.current && attemptRef.current?.status !== 'in-flight') {
        connectionLostRef.current = false
        attemptRef.current = null
        setReadinessGeneration(value => value + 1)
      }
    }

    const offDelegate = subscribeSessionSurfaceDelegate(delegateChanged)
    const offGateway = subscribeProfileGateway(profile, gatewayChanged)

    return () => {
      offDelegate()
      offGateway()
    }
  }, [profile])

  useEffect(() => {
    if (!valid) {
      return
    }

    retainSessionSurfaceReference(profile, storedSessionId)

    return () => releaseSessionSurfaceReference(profile, storedSessionId)
  }, [identityKey, profile, storedSessionId, valid])

  // Attempt ownership is intentionally non-rendering hot state, not a mirrored atom.
  // eslint-disable-next-line no-restricted-syntax
  useEffect(() => {
    if (!valid) {
      setBinding({ error: true, identityKey })

      return
    }

    const delegate = sessionSurfaceDelegate()

    if (!delegate) {
      setBinding({ identityKey })

      return
    }

    const generation = identityGenerationRef.current.value
    const currentAttempt = attemptRef.current

    if (
      currentAttempt?.generation === generation &&
      currentAttempt.retryGeneration === retryGeneration &&
      currentAttempt.status !== 'failed'
    ) {
      return
    }

    const transportGeneration = transportGenerationRef.current
    attemptRef.current = { generation, retryGeneration, status: 'in-flight', transportGeneration }
    setBinding({ identityKey })

    const bind = async () => {
      try {
        let id: string

        if (runtimeSessionId && staleRuntimeHintRef.current !== bindingIdentityKey) {
          try {
            id = await delegate.adoptSurface({ profile, runtimeSessionId, storedSessionId })
          } catch (error) {
            if (!isStaleSessionSurfaceRuntimeError(error)) {
              throw error
            }

            // Do not start durable recovery for an adoption result that already
            // belongs to an obsolete identity or transport generation.
            if (
              !mountedRef.current ||
              identityGenerationRef.current.value !== generation ||
              transportGenerationRef.current !== transportGeneration
            ) {
              throw error
            }

            // This hint is now known unusable for the current durable identity.
            // Retry and reconnect attempts resume directly instead of looping
            // through the same stale adoption forever.
            staleRuntimeHintRef.current = bindingIdentityKey
            id = await delegate.resumeSurface({ profile, storedSessionId })
          }
        } else {
          id = await delegate.resumeSurface({ profile, storedSessionId })
        }

        if (mountedRef.current && identityGenerationRef.current.value === generation) {
          if (transportGenerationRef.current !== transportGeneration) {
            attemptRef.current = null

            if (profileGatewayState(profile)?.connectionState === 'open') {
              connectionLostRef.current = false
              setReadinessGeneration(value => value + 1)
            }

            return
          }

          attemptRef.current = { generation, retryGeneration, status: 'succeeded', transportGeneration }
          bindSessionSurfaceRuntime(profile, storedSessionId, id)
          onRuntimeSessionId?.(id)
          setBinding({ identityKey, runtimeSessionId: id })
        }
      } catch {
        if (mountedRef.current && identityGenerationRef.current.value === generation) {
          if (transportGenerationRef.current !== transportGeneration) {
            attemptRef.current = null

            if (profileGatewayState(profile)?.connectionState === 'open') {
              connectionLostRef.current = false
              setReadinessGeneration(value => value + 1)
            }

            return
          }

          attemptRef.current = { generation, retryGeneration, status: 'failed', transportGeneration }
          setBinding({ error: true, identityKey })
        }
      }
    }

    void bind()
  }, [
    bindingIdentityKey,
    identityKey,
    onRuntimeSessionId,
    profile,
    readinessGeneration,
    retryGeneration,
    runtimeSessionId,
    storedSessionId,
    valid
  ])

  const currentBinding = binding.identityKey === identityKey ? binding : {}

  if (!valid) {
    return (
      <div className="grid h-full place-items-center p-4">
        <div className="max-w-[24rem] break-words text-center font-mono text-[11px] text-(--ui-text-quaternary)">
          Couldn't open this session
        </div>
      </div>
    )
  }

  if (currentBinding.error) {
    return (
      <div className="grid h-full place-items-center p-4">
        <div className="flex max-w-[24rem] flex-col items-center gap-2 text-center font-mono text-[11px] text-(--ui-text-quaternary)">
          <span>Couldn't open this session</span>
          <button
            className="rounded px-2 py-1 text-(--ui-text-secondary) hover:bg-(--ui-hover-bg)"
            onClick={() => {
              attemptRef.current = null
              setBinding({ identityKey })
              setRetryGeneration(value => value + 1)
            }}
            type="button"
          >
            Retry
          </button>
        </div>
      </div>
    )
  }

  if (!currentBinding.runtimeSessionId) {
    return (
      <div className="relative h-full">
        <CenteredThreadSpinner />
      </div>
    )
  }

  return (
    <SessionSurfaceChat
      profile={profile}
      runtimeSessionId={currentBinding.runtimeSessionId}
      storedSessionId={storedSessionId}
    />
  )
}

/** Public plugin surface. Runtime correlation stays encapsulated; only core
 * tile ownership code may observe the ephemeral runtime id. */
export function SessionSurface({ session }: SessionSurfaceProps) {
  return <SessionSurfaceCore {...session} />
}
