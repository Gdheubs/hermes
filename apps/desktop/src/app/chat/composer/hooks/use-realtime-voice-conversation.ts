import {
  type RealtimeFunctionCall,
  type RealtimeTokenGrant,
  RealtimeVoiceClient,
  VoiceSupervisorController
} from '@hermes/shared'
import { useCallback, useEffect, useRef, useState } from 'react'

import { notifyError } from '@/store/notifications'

import type { ConversationStatus } from './use-voice-conversation'

const CONSULT_POLL_MS = 500

interface PendingVoiceResponse {
  id: string
  pending: boolean
  text: string
  userText?: string | null
}

interface RealtimeConversationOptions {
  busy: boolean
  enabled: boolean
  /** Fresh ephemeral grant per dial; null → fall back to classic voice. */
  requestToken: () => Promise<RealtimeTokenGrant | null>
  submitTask: (text: string) => Promise<void>
  onInterrupt?: () => Promise<void> | void
  onFatalError?: () => void
  onStopWord?: () => void
  isStopWord: (text: string) => boolean
  pendingResponse: () => PendingVoiceResponse | null
  consumePendingResponse: () => void
  beforeMicOpen?: () => Promise<void> | void
  failureLabel: string
}

/** xAI S2S supervisor loop — same surface as useVoiceConversation. */
export function useRealtimeVoiceConversation({
  busy,
  enabled,
  requestToken,
  submitTask,
  onInterrupt,
  onFatalError,
  onStopWord,
  isStopWord,
  pendingResponse,
  consumePendingResponse,
  beforeMicOpen,
  failureLabel
}: RealtimeConversationOptions) {
  const [status, setStatus] = useState<ConversationStatus>('idle')
  const [muted, setMuted] = useState(false)
  const [level, setLevel] = useState(0)
  const clientRef = useRef<RealtimeVoiceClient | null>(null)
  const controllerRef = useRef<VoiceSupervisorController | null>(null)
  const generationRef = useRef(0)
  const busyRef = useRef(busy)
  busyRef.current = busy

  const optionsRef = useRef({
    requestToken,
    submitTask,
    onInterrupt,
    onFatalError,
    onStopWord,
    isStopWord,
    pendingResponse,
    consumePendingResponse,
    beforeMicOpen,
    failureLabel
  })

  optionsRef.current = {
    requestToken,
    submitTask,
    onInterrupt,
    onFatalError,
    onStopWord,
    isStopWord,
    pendingResponse,
    consumePendingResponse,
    beforeMicOpen,
    failureLabel
  }

  const teardown = useCallback(() => {
    generationRef.current += 1
    controllerRef.current?.failActiveConsult('Voice session ended.')
    controllerRef.current = null
    clientRef.current?.close()
    clientRef.current = null
    setStatus('idle')
    setLevel(0)
  }, [])

  const completeConsult = useCallback(() => {
    const controller = controllerRef.current
    const client = clientRef.current

    if (!controller || !client || !controller.consultActive) {
      return
    }

    const reply = optionsRef.current.pendingResponse()

    if (!reply || reply.pending || busyRef.current) {
      return
    }

    const userText = reply.userText?.trim() ?? ''

    if (userText && !controller.ownsTurn(userText)) {
      optionsRef.current.consumePendingResponse()

      return
    }

    optionsRef.current.consumePendingResponse()
    controller.onTurnComplete(userText || controller.currentTask || '', reply.text)
  }, [])

  const handleFunctionCall = useCallback((call: RealtimeFunctionCall) => {
    return controllerRef.current?.onFunctionCall(call.name, call.callId, call.args)
  }, [])

  const start = useCallback(async () => {
    if (clientRef.current) {
      return
    }

    const generation = ++generationRef.current
    setStatus('transcribing')

    try {
      await optionsRef.current.beforeMicOpen?.()
      const grant = await optionsRef.current.requestToken()

      if (generation !== generationRef.current) {
        return
      }

      if (!grant) {
        teardown()

        return
      }

      const client = new RealtimeVoiceClient()
      clientRef.current = client
      controllerRef.current = new VoiceSupervisorController(client, {
        submit: async task => {
          await optionsRef.current.submitTask(task)

          return true
        },
        interrupt: () => optionsRef.current.onInterrupt?.(),
        isBusy: () => busyRef.current,
        isQueueEmpty: () => !optionsRef.current.pendingResponse()
      })
      await client.connect(grant, {
        onFunctionCall: handleFunctionCall,
        onLevel: value => setLevel(value),
        onUserTranscript: text => {
          if (optionsRef.current.isStopWord(text)) {
            optionsRef.current.onStopWord?.()
          }
        },
        onStatus: (clientStatus, detail) => {
          if (generation !== generationRef.current) {
            return
          }

          if (clientStatus === 'speaking') {
            setStatus('speaking')
          } else if (clientStatus === 'listening') {
            setStatus(controllerRef.current?.consultActive || busyRef.current ? 'thinking' : 'listening')
          } else if (clientStatus === 'error') {
            notifyError(new Error(detail ?? 'realtime voice error'), optionsRef.current.failureLabel)
          } else if (clientStatus === 'closed' && clientRef.current) {
            teardown()
            optionsRef.current.onFatalError?.()
          }
        }
      })

      if (generation !== generationRef.current) {
        client.close()

        return
      }

      setStatus('listening')
    } catch (error) {
      if (generation === generationRef.current) {
        teardown()
        notifyError(error, optionsRef.current.failureLabel)
        optionsRef.current.onFatalError?.()
      }
    }
  }, [handleFunctionCall, teardown])

  const end = useCallback(async () => {
    teardown()
  }, [teardown])

  useEffect(() => {
    if (!enabled) {
      return
    }

    const timer = setInterval(() => {
      if (controllerRef.current?.consultActive) {
        completeConsult()
      }
    }, CONSULT_POLL_MS)

    return () => clearInterval(timer)
  }, [completeConsult, enabled])

  useEffect(() => {
    if (!enabled || !clientRef.current) {
      return
    }

    if (busy || controllerRef.current?.consultActive) {
      setStatus(prev => (prev === 'speaking' ? prev : 'thinking'))
    } else {
      setStatus(prev => (prev === 'thinking' ? 'listening' : prev))
    }
  }, [busy, enabled])

  useEffect(() => {
    if (enabled) {
      void start()
    } else {
      teardown()
    }
  }, [enabled, start, teardown])

  useEffect(() => () => teardown(), [teardown])

  const toggleMute = useCallback(() => {
    setMuted(prev => {
      const next = !prev
      clientRef.current?.setMuted(next)

      return next
    })
  }, [])

  const stopTurn = useCallback(() => undefined, [])

  return { end, level, muted, start, status, stopTurn, toggleMute }
}
