import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { group } from '@/components/pane-shell/tree/model'
import { closeOtherTreeTabs, declareDefaultTree, registerPaneCloser } from '@/components/pane-shell/tree/store'
import { PaneTab, PaneTabLabel } from '@/components/ui/pane-tab'
import { $sessionStates, $sessionTiles } from '@/store/session-states'

import { requestCloseSessionTile, SessionTabMenu, SessionTileCloseConfirm } from './session-tile'

const busyTileState: ClientSessionState = {
  adoptedRunningTurn: false,
  awaitingResponse: false,
  branch: '',
  busy: true,
  cwd: '',
  fast: false,
  interimBoundaryPending: false,
  interrupted: false,
  messages: [],
  model: '',
  needsInput: false,
  pendingBranchGroup: null,
  personality: '',
  provider: '',
  reasoningEffort: '',
  sawAssistantPayload: false,
  serviceTier: '',
  storedSessionId: 'busy-session',
  streamId: null,
  turnStartedAt: null,
  usage: null,
  yolo: false
}

function expectClosePromise(value: unknown): Promise<void> {
  expect(value).toBeInstanceOf(Promise)

  return value as Promise<void>
}

function mockDialogExitAnimation() {
  vi.stubGlobal('CSS', { escape: (value: string) => value })
  const nativeGetComputedStyle = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => {
    const styles = nativeGetComputedStyle(element)

    if (element instanceof HTMLElement && element.dataset.slot === 'dialog-content') {
      return new Proxy(styles, {
        get(target, property, receiver) {
          if (property === 'animationName') {
            return element.dataset.state === 'closed' ? 'close-running-tab' : 'open-running-tab'
          }

          return Reflect.get(target, property, receiver)
        }
      }) as CSSStyleDeclaration
    }

    return styles
  })
}

describe('busy session tile close confirmation', () => {
  beforeEach(() => {
    $sessionStates.set({ 'busy-runtime': busyTileState })
    $sessionTiles.set([{ runtimeId: 'busy-runtime', storedSessionId: 'busy-session' }])
  })

  afterEach(() => {
    cleanup()
    registerPaneCloser('session-tile:busy-session')
    registerPaneCloser('session-tile:other-busy-session')
    vi.restoreAllMocks()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    $sessionStates.set({})
    $sessionTiles.set([])
  })

  it('opens the session tab menu when right-clicking its close control', async () => {
    render(
      <SessionTabMenu
        onClose={vi.fn()}
        storedSessionId="busy-session"
        tabPaneId="session-tile:busy-session"
      >
        <PaneTab onClose={vi.fn()}>
          <PaneTabLabel>Busy session</PaneTabLabel>
        </PaneTab>
      </SessionTabMenu>
    )

    const closeButton = screen.getByRole('button', { name: 'Close tab' })
    fireEvent.pointerDown(closeButton, { button: 2, pointerType: 'mouse' })
    fireEvent.contextMenu(closeButton)

    expect(await screen.findByRole('menu')).toBeTruthy()
    expect(screen.getByRole('menuitem', { name: /^close$/i })).toBeTruthy()
  })

  it('keeps a busy close pending through StrictMode dialog mounting', async () => {
    render(
      <StrictMode>
        <SessionTileCloseConfirm />
      </StrictMode>
    )

    let close: Promise<void> | undefined
    let settled = false
    act(() => {
      close = expectClosePromise(requestCloseSessionTile('busy-session'))
      void close.then(
        () => {
          settled = true
        },
        () => {
          settled = true
        }
      )
    })

    expect(await screen.findByRole('dialog', { name: 'Close running tab?' })).toBeTruthy()
    await act(async () => {
      await Promise.resolve()
    })

    expect(settled).toBe(false)
    expect($sessionTiles.get().map(tile => tile.storedSessionId)).toEqual(['busy-session'])
  })

  it('keeps a busy close pending until the confirmation is accepted', async () => {
    render(<SessionTileCloseConfirm />)

    let close: Promise<void> | undefined
    act(() => {
      close = expectClosePromise(requestCloseSessionTile('busy-session'))
    })

    expect(await screen.findByRole('dialog', { name: 'Close running tab?' })).toBeTruthy()
    await Promise.resolve()
    expect($sessionTiles.get().map(tile => tile.storedSessionId)).toEqual(['busy-session'])

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Close tab' }))
      await close!
    })

    expect($sessionTiles.get()).toEqual([])
  })

  it('rejects the pending close when the confirmation is canceled', async () => {
    render(<SessionTileCloseConfirm />)

    let close: Promise<void> | undefined
    act(() => {
      close = expectClosePromise(requestCloseSessionTile('busy-session'))
    })

    expect(await screen.findByRole('dialog', { name: 'Close running tab?' })).toBeTruthy()
    vi.useFakeTimers()
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    })

    await act(async () => {
      await vi.runOnlyPendingTimersAsync()
    })

    await expect(close!).rejects.toThrow('Session tab close canceled')
    expect($sessionTiles.get().map(tile => tile.storedSessionId)).toEqual(['busy-session'])
  })

  it('rejects a pending close when its confirmation unmounts', async () => {
    const view = render(<SessionTileCloseConfirm />)

    let close: Promise<void> | undefined
    act(() => {
      close = expectClosePromise(requestCloseSessionTile('busy-session'))
    })

    expect(await screen.findByRole('dialog', { name: 'Close running tab?' })).toBeTruthy()
    view.unmount()

    await expect(close!).rejects.toThrow('Session tab close canceled')
  })

  it('keeps a confirmed busy close pending through the dialog exit animation', async () => {
    mockDialogExitAnimation()

    render(<SessionTileCloseConfirm />)

    let close: Promise<void> | undefined
    let settled = false
    act(() => {
      close = expectClosePromise(requestCloseSessionTile('busy-session'))
      void close.then(() => {
        settled = true
      })
    })

    expect(await screen.findByRole('dialog', { name: 'Close running tab?' })).toBeTruthy()
    vi.useFakeTimers()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Close tab' }))
      await vi.advanceTimersByTimeAsync(600)
    })

    const dialog = screen.getByRole('dialog', { name: 'Close running tab?' })
    expect(dialog.dataset.state).toBe('closed')
    expect(settled).toBe(false)

    await act(async () => {
      const animationEnd = new Event('animationend', { bubbles: true })
      Object.defineProperty(animationEnd, 'animationName', { value: 'close-running-tab' })
      dialog.dispatchEvent(animationEnd)
      await Promise.resolve()
    })

    await act(async () => {
      await vi.runOnlyPendingTimersAsync()
    })

    expect(settled).toBe(true)
    await expect(close).resolves.toBeUndefined()
  })

  it('serializes busy session confirmations during Close Others', async () => {
    const otherBusyTileState = { ...busyTileState, storedSessionId: 'other-busy-session' }
    $sessionStates.set({ 'busy-runtime': busyTileState, 'other-busy-runtime': otherBusyTileState })
    $sessionTiles.set([
      { runtimeId: 'busy-runtime', storedSessionId: 'busy-session' },
      { runtimeId: 'other-busy-runtime', storedSessionId: 'other-busy-session' }
    ])
    declareDefaultTree(
      group(['workspace', 'session-tile:busy-session', 'session-tile:other-busy-session'], {
        active: 'workspace',
        id: 'busy-close-group'
      })
    )
    registerPaneCloser('session-tile:busy-session', () => requestCloseSessionTile('busy-session'))
    registerPaneCloser('session-tile:other-busy-session', () => requestCloseSessionTile('other-busy-session'))
    render(<SessionTileCloseConfirm />)

    const completion = expectClosePromise(closeOtherTreeTabs('workspace'))
    expect(await screen.findByRole('dialog', { name: 'Close running tab?' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Close tab' }))

    await waitFor(() => {
      expect($sessionTiles.get().map(tile => tile.storedSessionId)).toEqual(['other-busy-session'])
      expect(screen.getByRole('dialog', { name: 'Close running tab?' }).dataset.state).toBe('open')
      expect(screen.getByRole('button', { name: /^(Close tab|Done)$/ }).hasAttribute('disabled')).toBe(false)
    })

    fireEvent.click(screen.getByRole('button', { name: /^(Close tab|Done)$/ }))
    await completion
    expect($sessionTiles.get()).toEqual([])
  })
})
