import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import {
  type Dispatch,
  type PropsWithChildren,
  type SetStateAction,
  useLayoutEffect,
  useState
} from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import { $clarifyRequests } from '@/store/clarify'
import type { ComposerAttachment } from '@/store/composer'
import { $gateway } from '@/store/gateway'
import {
  clearAllPrompts,
  hasBlockingPromptRequest,
  setApprovalRequest,
  setSecretRequest,
  setSudoRequest
} from '@/store/prompts'

import { type ComposerTarget, requestComposerSubmit } from '../focus'
import { ComposerScopeProvider, ComposerSurfaceProvider, MAIN_COMPOSER_SCOPE } from '../scope'

import { useComposerSubmit } from './use-composer-submit'

interface SubmitHarnessOptions {
  attachments?: ComposerAttachment[]
  busy?: boolean
  compacting?: boolean
  inputDisabled?: boolean
  sessionKey?: string | null
  surfaceId?: string | null
  submitOnHide?: boolean
  target?: ComposerTarget
  text?: string
  visible?: boolean
}

let surfaceSequence = 0

function renderSubmitHook({
  attachments = [],
  busy = false,
  compacting = false,
  inputDisabled = false,
  sessionKey = 'stored-session',
  surfaceId,
  submitOnHide = false,
  target = 'main',
  text = '',
  visible = true
}: SubmitHarnessOptions = {}) {
  const resolvedSurfaceId = surfaceId === undefined ? `test-surface-${++surfaceSequence}` : surfaceId
  const draftRef = { current: text }
  const editor = window.document.createElement('div')
  editor.dataset.slot = 'composer-rich-input'
  editor.textContent = text
  const editorRef = { current: editor }
  const onCancel = vi.fn()
  const onSteer = vi.fn(async () => true)
  const onSubmit = vi.fn(async () => true)
  const queueCurrentDraft = vi.fn(() => true)
  let updatePaneVisible: Dispatch<SetStateAction<boolean>> | undefined

  const clearDraft = vi.fn(() => {
    draftRef.current = ''
    editorRef.current!.textContent = ''
  })

  const Wrapper = ({ children }: PropsWithChildren) => {
    const [paneVisible, setPaneVisible] = useState(visible)
    updatePaneVisible = setPaneVisible

    useLayoutEffect(() => {
      if (submitOnHide && !paneVisible) {
        requestComposerSubmit('ship while hiding', { target })
      }
    }, [paneVisible])

    return (
      <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, target }}>
        <ComposerSurfaceProvider value={resolvedSurfaceId}>
          <PaneVisibleContext.Provider value={paneVisible}>
            <div
              data-composer-surface-id={resolvedSurfaceId ?? undefined}
              data-composer-target={target}
              data-pane-hidden={paneVisible ? undefined : ''}
            >
              {children}
            </div>
          </PaneVisibleContext.Provider>
        </ComposerSurfaceProvider>
      </ComposerScopeProvider>
    )
  }

  const hook = renderHook(
    () =>
      useComposerSubmit({
        activeQueueSessionKey: sessionKey,
        activeQueueSessionKeyRef: { current: sessionKey },
        attachments,
        busy,
        compacting,
        clearDraft,
        disabled: false,
        draftRef,
        drainNextQueued: vi.fn(async () => false),
        editorRef,
        exitQueuedEdit: vi.fn(() => false),
        focusInput: vi.fn(),
        inputDisabled,
        loadIntoComposer: vi.fn(),
        onCancel,
        onSteer,
        onSubmit,
        queueCurrentDraft,
        queueEdit: null,
        queuedPrompts: [],
        sessionId: 'runtime-session',
        setComposerText: vi.fn(),
        stashAt: vi.fn()
      }),
    { wrapper: Wrapper }
  )

  return {
    clearDraft,
    hook,
    onCancel,
    onSteer,
    onSubmit,
    queueCurrentDraft,
    setPaneVisible(nextVisible: boolean) {
      if (!updatePaneVisible) {
        throw new Error('Pane visibility setter was not initialized')
      }

      updatePaneVisible(nextVisible)
    }
  }
}

describe('useComposerSubmit external request routing', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('submits only through the visible composer whose target matches the request', async () => {
    const visibleMain = renderSubmitHook()
    const hiddenMain = renderSubmitHook({ visible: false })
    const visibleTile = renderSubmitHook({ target: 'tile:other-session' })

    requestComposerSubmit('ship this branch', { target: 'main' })

    await waitFor(() =>
      expect(visibleMain.onSubmit).toHaveBeenCalledWith('ship this branch', { composerScope: 'stored-session' })
    )
    expect(hiddenMain.onSubmit).not.toHaveBeenCalled()
    expect(visibleTile.onSubmit).not.toHaveBeenCalled()
  })

  it('does not use a stale visible subscription while the pane is being hidden', () => {
    const main = renderSubmitHook({ submitOnHide: true })

    const runImmediately = ((handler: TimerHandler) => {
      if (typeof handler === 'function') {
        handler()
      }

      return 0
    }) as unknown as typeof window.setTimeout

    vi.spyOn(window, 'setTimeout').mockImplementationOnce(runImmediately)

    act(() => main.setPaneVisible(false))

    expect(main.onSubmit).not.toHaveBeenCalled()
  })

  it('submits to the session visible at click time even when the same click switches tabs', async () => {
    const hiddenA = renderSubmitHook({ sessionKey: 'session-a', visible: false })
    const visibleB = renderSubmitHook({ sessionKey: 'session-b' })

    act(() => {
      requestComposerSubmit('ship session B', { target: 'main' })
      visibleB.setPaneVisible(false)
      hiddenA.setPaneVisible(true)
    })

    await waitFor(() =>
      expect(visibleB.onSubmit).toHaveBeenCalledWith('ship session B', { composerScope: 'session-b' })
    )
    expect(hiddenA.onSubmit).not.toHaveBeenCalled()
  })

  it('uses the captured surface id when two main composers both report visible', async () => {
    const firstMain = renderSubmitHook({ sessionKey: 'session-first' })
    const secondMain = renderSubmitHook({ sessionKey: 'session-second' })

    requestComposerSubmit('ship exactly one session', { target: 'main' })

    await waitFor(() =>
      expect(firstMain.onSubmit).toHaveBeenCalledWith('ship exactly one session', {
        composerScope: 'session-first'
      })
    )
    expect(secondMain.onSubmit).not.toHaveBeenCalled()
  })

  it('does not fan out when visible composers do not have queue session keys yet', async () => {
    const firstNewSession = renderSubmitHook({ sessionKey: null })
    const secondNewSession = renderSubmitHook({ sessionKey: null })

    act(() => {
      requestComposerSubmit('ship the visible new session', { target: 'main' })
    })

    await waitFor(() => expect(firstNewSession.onSubmit).toHaveBeenCalledTimes(1))
    expect(secondNewSession.onSubmit).not.toHaveBeenCalled()
  })

  it('fails closed when the visible composer has no surface id', () => {
    const unidentifiedMain = renderSubmitHook({ surfaceId: null })

    requestComposerSubmit('do not broadcast this', { target: 'main' })

    expect(unidentifiedMain.onSubmit).not.toHaveBeenCalled()
  })

  it('does not submit through a composer whose input is disabled', () => {
    const disabledMain = renderSubmitHook({ inputDisabled: true })

    requestComposerSubmit('do not send this', { target: 'main' })

    expect(disabledMain.onSubmit).not.toHaveBeenCalled()
  })
})

describe('useComposerSubmit busy-turn routing', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('treats a payload mid-turn as send (steer), not stop', async () => {
    const { hook, onCancel, onSteer, onSubmit, queueCurrentDraft } = renderSubmitHook({
      busy: true,
      text: 'change course'
    })

    act(() => {
      hook.result.current.submitDraft()
    })

    await waitFor(() => expect(onSteer).toHaveBeenCalledWith('change course'))
    expect(queueCurrentDraft).not.toHaveBeenCalled()
    expect(onCancel).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('queues a plain-text follow-up while the active turn is compacting', () => {
    const { hook, onCancel, onSteer, onSubmit, queueCurrentDraft } = renderSubmitHook({
      busy: true,
      compacting: true,
      text: 'wait for the summary'
    })

    act(() => {
      hook.result.current.submitDraft()
    })

    expect(queueCurrentDraft).toHaveBeenCalledTimes(1)
    expect(onSteer).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('runs slash commands immediately while busy', async () => {
    const { clearDraft, hook, onCancel, onSteer, onSubmit, queueCurrentDraft } = renderSubmitHook({
      busy: true,
      text: '/compress preserve context'
    })

    act(() => {
      hook.result.current.submitDraft()
    })

    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith('/compress preserve context', { composerScope: 'stored-session' })
    )
    expect(clearDraft).toHaveBeenCalledTimes(1)
    expect(onSteer).not.toHaveBeenCalled()
    expect(queueCurrentDraft).not.toHaveBeenCalled()
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('queues an attachment-bearing follow-up while busy', () => {
    const attachment: ComposerAttachment = { id: 'doc', kind: 'file', label: 'notes.txt' }

    const { hook, onCancel, onSteer, onSubmit, queueCurrentDraft } = renderSubmitHook({
      attachments: [attachment],
      busy: true,
      text: 'read this'
    })

    act(() => {
      hook.result.current.submitDraft()
    })

    expect(queueCurrentDraft).toHaveBeenCalledTimes(1)
    expect(onSteer).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('stops an active turn only with an empty composer', () => {
    const { hook, onCancel, onSteer, onSubmit, queueCurrentDraft } = renderSubmitHook({ busy: true })

    act(() => {
      hook.result.current.submitDraft()
    })

    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onSteer).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
    expect(queueCurrentDraft).not.toHaveBeenCalled()
  })

  it('submits a normal turn while idle', async () => {
    const { hook, onCancel, onSteer, onSubmit, queueCurrentDraft } = renderSubmitHook({ text: 'ordinary question' })

    act(() => {
      hook.result.current.submitDraft()
    })

    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith('ordinary question', {
        attachments: [],
        composerScope: 'stored-session'
      })
    )
    expect(onSteer).not.toHaveBeenCalled()
    expect(queueCurrentDraft).not.toHaveBeenCalled()
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('threads the loaded composer scope through onSubmit for the #59305 submit-time guard', async () => {
    const { hook, onSubmit } = renderSubmitHook({ text: 'hello' })

    act(() => {
      hook.result.current.submitDraft()
    })

    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith('hello', expect.objectContaining({ composerScope: 'stored-session' }))
    )
  })
})

describe('useComposerSubmit with a clarify parked on the session', () => {
  const gatewayRequest = vi.fn(async () => ({ ok: true }))

  const parkClarify = (sessionId: string) => {
    $clarifyRequests.set({
      [sessionId]: {
        requestId: `req-${sessionId}`,
        question: 'which one?',
        choices: ['a', 'b'],
        multiSelect: false,
        sessionId
      }
    })
    $gateway.set({ request: gatewayRequest } as unknown as ReturnType<typeof $gateway.get>)
  }

  afterEach(() => {
    cleanup()
    gatewayRequest.mockClear()
    $clarifyRequests.set({})
    $gateway.set(null)
    vi.restoreAllMocks()
  })

  it('skips the question and still sends the typed message on an idle session', async () => {
    parkClarify('runtime-session')
    const { hook, onSubmit } = renderSubmitHook({ text: 'actually do this instead' })

    act(() => {
      hook.result.current.submitDraft()
    })

    await waitFor(() =>
      expect(gatewayRequest).toHaveBeenCalledWith('clarify.respond', {
        request_id: 'req-runtime-session',
        answer: ''
      })
    )
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith('actually do this instead', expect.objectContaining({ attachments: [] }))
    )
    expect($clarifyRequests.get()['runtime-session']).toBeUndefined()
  })

  it('skips the question before steering a busy turn', async () => {
    parkClarify('runtime-session')
    const { hook, onSteer } = renderSubmitHook({ busy: true, text: 'change course' })

    act(() => {
      hook.result.current.submitDraft()
    })

    await waitFor(() => expect(onSteer).toHaveBeenCalledWith('change course'))
    expect(gatewayRequest).toHaveBeenCalledWith('clarify.respond', { request_id: 'req-runtime-session', answer: '' })
  })

  it('leaves the question alone for an empty Enter (Stop, not an answer)', () => {
    parkClarify('runtime-session')
    const { hook, onCancel } = renderSubmitHook({ busy: true })

    act(() => {
      hook.result.current.submitDraft()
    })

    expect(gatewayRequest).not.toHaveBeenCalled()
    expect($clarifyRequests.get()['runtime-session']).toBeDefined()
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it("leaves another session's question alone", async () => {
    parkClarify('other-session')
    const { hook, onSubmit } = renderSubmitHook({ text: 'unrelated message' })

    act(() => {
      hook.result.current.submitDraft()
    })

    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(gatewayRequest).not.toHaveBeenCalled()
    expect($clarifyRequests.get()['other-session']).toBeDefined()
  })
})

describe('useComposerSubmit with a blocking prompt parked on the session', () => {
  // Typing cannot answer approval/sudo/secret prompts, so the busy submit must
  // route text to the QUEUE — a steer would sit undelivered behind the blocked
  // tool batch, and interrupting to force it through resolves the prompt empty
  // and ends the turn as "Operation interrupted." with the message lost.
  afterEach(() => {
    cleanup()
    clearAllPrompts()
    vi.restoreAllMocks()
  })

  it('queues a busy text follow-up instead of steering while an approval is pending', () => {
    setApprovalRequest({ command: 'rm -rf /tmp/x', description: 'dangerous', sessionId: 'runtime-session' })

    const { hook, onCancel, onSteer, queueCurrentDraft } = renderSubmitHook({
      busy: true,
      text: 'and also fix the padding'
    })

    act(() => {
      hook.result.current.submitDraft()
    })

    expect(queueCurrentDraft).toHaveBeenCalledTimes(1)
    expect(onSteer).not.toHaveBeenCalled()
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('queues while a sudo prompt is pending', () => {
    setSudoRequest({ requestId: 'sudo-1', sessionId: 'runtime-session' })

    const { hook, onSteer, queueCurrentDraft } = renderSubmitHook({ busy: true, text: 'next thing' })

    act(() => {
      hook.result.current.submitDraft()
    })

    expect(queueCurrentDraft).toHaveBeenCalledTimes(1)
    expect(onSteer).not.toHaveBeenCalled()
  })

  it('queues while a secret prompt is pending', () => {
    setSecretRequest({ envVar: 'API_KEY', prompt: 'key?', requestId: 'sec-1', sessionId: 'runtime-session' })

    const { hook, onSteer, queueCurrentDraft } = renderSubmitHook({ busy: true, text: 'next thing' })

    act(() => {
      hook.result.current.submitDraft()
    })

    expect(queueCurrentDraft).toHaveBeenCalledTimes(1)
    expect(onSteer).not.toHaveBeenCalled()
  })

  it('still runs slash commands inline', async () => {
    setApprovalRequest({ command: 'rm -rf /tmp/x', description: 'dangerous', sessionId: 'runtime-session' })

    const { hook, onSteer, onSubmit, queueCurrentDraft } = renderSubmitHook({ busy: true, text: '/status' })

    act(() => {
      hook.result.current.submitDraft()
    })

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('/status', { composerScope: 'stored-session' }))
    expect(queueCurrentDraft).not.toHaveBeenCalled()
    expect(onSteer).not.toHaveBeenCalled()
  })

  it("ignores another session's blocking prompt and still steers", async () => {
    setApprovalRequest({ command: 'ls', description: 'other', sessionId: 'other-session' })

    const { hook, onSteer, queueCurrentDraft } = renderSubmitHook({ busy: true, text: 'change course' })

    act(() => {
      hook.result.current.submitDraft()
    })

    await waitFor(() => expect(onSteer).toHaveBeenCalledWith('change course'))
    expect(queueCurrentDraft).not.toHaveBeenCalled()
  })

  it('leaves the prompt pending — queueing must not resolve or dismiss it', () => {
    setApprovalRequest({ command: 'rm -rf /tmp/x', description: 'dangerous', sessionId: 'runtime-session' })

    const { hook } = renderSubmitHook({ busy: true, text: 'follow-up' })

    act(() => {
      hook.result.current.submitDraft()
    })

    // The approval card is still the turn's owner; only its own buttons answer it.
    expect(hasBlockingPromptRequest('runtime-session')).toBe(true)
  })
})
