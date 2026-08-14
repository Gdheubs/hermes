import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { closeActiveTab } from '@/app/chat/close-tab'
import { requestCloseSessionTile, SessionTileCloseConfirm } from '@/app/chat/session-tile'
import type { ClientSessionState } from '@/app/types'
import { registry } from '@/contrib/registry'
import { $sessionStates, $sessionTiles } from '@/store/session-states'

import { $layoutEditMode } from '../../edit-mode'
import { findGroup, group, split } from '../model'
import {
  $collapsedTreeSides,
  $dismissedPanes,
  $hiddenTreePanes,
  $layoutTree,
  cycleTreeTabInFocusedZone,
  declareDefaultTree,
  dismissTreePane,
  markCollapsePane,
  noteActiveTreeGroup,
  noteHoveredTreeGroup,
  registerPaneCloser,
  setTreeGroupMinimized,
  setTreePaneHidden,
  setTreeSideCollapsed
} from '../store'
import { $treeFocusRequest, clearTreeFocusRequest, requestTreeFocusAfterClose, settleTreeFocusAfterClose } from '../tree-focus'

import { TreeGroup } from './tree-group'

import { LayoutTreeRoot } from '.'

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const busySessionState: ClientSessionState = {
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

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', TestResizeObserver)
  vi.stubGlobal('CSS', { ...globalThis.CSS, escape: (value: string) => value })
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
  HTMLElement.prototype.scrollIntoView ??= () => undefined
})

const disposers: (() => void)[] = []

beforeEach(() => {
  window.localStorage.clear()
  $layoutEditMode.set(false)
  $collapsedTreeSides.set(new Set())
  $dismissedPanes.set(new Set())
  $hiddenTreePanes.set(new Set())
  $layoutTree.set(null)
  $treeFocusRequest.set(null)
  $sessionStates.set({})
  $sessionTiles.set([])

  for (const [id, data] of [
    ['workspace', { placement: 'main', uncloseable: true }],
    ['files', { placement: 'left' }],
    ['terminal', { placement: 'bottom' }],
    ['logs', { placement: 'bottom' }],
    ['busy-session-pane', { placement: 'bottom' }],
    ['delayed-close-test', { placement: 'main' }],
    ['focused-side-a', { placement: 'main' }],
    ['focused-side-b', { placement: 'main' }],
    ['plain-a', { placement: 'bottom' }],
    ['plain-b', { placement: 'bottom' }],
    ['side-close-sibling', { placement: 'right' }],
    ['side-close-test', { placement: 'right' }]
  ] as const) {
    disposers.push(
      registry.register({
        area: 'panes',
        data,
        id,
        render: () =>
          id === 'workspace' ? (
            <>
              <button type="button">Workspace action</button>
              <div contentEditable data-slot="composer-rich-input" role="textbox" suppressContentEditableWarning />
            </>
          ) : null,
        title: id
      })
    )
  }

  markCollapsePane('terminal')
  markCollapsePane('logs')
})

afterEach(() => {
  cleanup()
  $layoutEditMode.set(false)
  $collapsedTreeSides.set(new Set())
  $layoutTree.set(null)
  $treeFocusRequest.set(null)
  $sessionStates.set({})
  $sessionTiles.set([])
  registerPaneCloser('delayed-close-test')
  registerPaneCloser('busy-session-pane')
  disposers.splice(0).forEach(dispose => dispose())
})

const zoneAt = (index: number) => {
  const node = $layoutTree.get()!

  return (node.type === 'split' ? node.children[index] : node) as never
}

const tabControl = (paneId: string) =>
  window.document.querySelector<HTMLElement>(`[data-tree-tab="${paneId}"] [data-pane-tab-control="true"]`)

describe('TreeGroup tab keyboard interaction', () => {
  it('does not let an older focus request clear a newer one', () => {
    const older = requestTreeFocusAfterClose('terminal')
    settleTreeFocusAfterClose(older)
    const newer = requestTreeFocusAfterClose('logs')

    clearTreeFocusRequest(older)
    expect($treeFocusRequest.get()).toBe(newer)

    clearTreeFocusRequest(newer)
    expect($treeFocusRequest.get()).toBeNull()
  })

  it('associates rendered tabs with their kept-alive tab panels', () => {
    declareDefaultTree(
      split('column', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'logs'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    render(<LayoutTreeRoot />)

    for (const paneId of ['terminal', 'logs']) {
      const tab = tabControl(paneId)!
      const panelId = tab.getAttribute('aria-controls')
      const panel = window.document.getElementById(panelId!)

      expect(tab.id).not.toBe('')
      expect(panelId).toBeTruthy()
      expect(panel?.getAttribute('aria-labelledby')).toBe(tab.id)
      expect(panel?.getAttribute('role')).toBe('tabpanel')
    }
  })

  it.each(['column', 'row'] as const)(
    'keeps minimized %s tabs selected and associated with hidden panels',
    parentAxis => {
      declareDefaultTree(
        split(parentAxis, [
          group(['workspace'], { active: 'workspace', id: 'grp-main' }),
          group(['terminal', 'logs'], { active: 'terminal', id: 'grp-tools' })
        ])
      )
      setTreeGroupMinimized('grp-tools', true)
      render(<TreeGroup node={zoneAt(1)} parentAxis={parentAxis} />)

      const tab = tabControl('terminal')!
      const panel = window.document.getElementById(tab.getAttribute('aria-controls')!)

      expect(tab.getAttribute('aria-selected')).toBe('true')
      expect(panel?.getAttribute('aria-labelledby')).toBe(tab.id)
      expect(panel?.getAttribute('role')).toBe('tabpanel')
      expect(panel?.hidden).toBe(true)
    }
  )

  it('recovers focus after ⌘W closes a focused tool tab', async () => {
    declareDefaultTree(
      split('column', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'logs'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    render(<LayoutTreeRoot />)

    const terminal = tabControl('terminal')!
    terminal.focus()
    noteActiveTreeGroup('grp-tools')
    act(() => {
      expect(closeActiveTab()).toBe(true)
    })

    await waitFor(() => {
      expect(tabControl('terminal')).toBeNull()
      expect(window.document.activeElement).toBe(tabControl('logs'))
    })
  })

  it('recovers focus after ⌘W closes a focused session tab', async () => {
    declareDefaultTree(
      split('column', [
        group(['workspace', 'delayed-close-test'], { active: 'delayed-close-test', id: 'grp-main' }),
        group(['terminal'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    render(<LayoutTreeRoot />)

    const sessionTab = tabControl('delayed-close-test')!
    sessionTab.focus()
    noteActiveTreeGroup('grp-main')
    act(() => {
      expect(closeActiveTab()).toBe(true)
    })

    await waitFor(() => {
      expect(tabControl('delayed-close-test')).toBeNull()
      expect(window.document.activeElement).toBe(tabControl('terminal'))
    })
  })

  it('keeps deferred global ⌘W recovery in the focused split-off tab group', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace', 'delayed-close-test'], { active: 'workspace', id: 'grp-main' }),
        group(['focused-side-a', 'focused-side-b'], { active: 'focused-side-b', id: 'grp-side' })
      ])
    )
    let closeCompletion: Promise<void> | undefined

    let finishClose = () => {}
    registerPaneCloser(
      'focused-side-b',
      () => {
        closeCompletion = new Promise<void>(resolve => {
          finishClose = () => {
            dismissTreePane('focused-side-b')
            resolve()
          }
        })

        return closeCompletion
      }
    )
    render(<LayoutTreeRoot />)

    try {
      const focusedSide = tabControl('focused-side-b')!
      focusedSide.focus()
      noteActiveTreeGroup('grp-side')
      expect(tabControl('workspace')?.getAttribute('aria-selected')).toBe('true')
      act(() => {
        expect(closeActiveTab()).toBe(true)
      })
      expect(tabControl('focused-side-b')).toBe(focusedSide)

      await act(async () => {
        finishClose()
        await closeCompletion
      })

      await waitFor(() => {
        expect(tabControl('focused-side-b')).toBeNull()
        expect(window.document.activeElement).toBe(tabControl('focused-side-a'))
      })
    } finally {
      act(() => registerPaneCloser('focused-side-b'))
    }
  })

  it('keeps deferred global ⌘W recovery in the focused group when its raw successor is hidden', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace', 'delayed-close-test'], { active: 'workspace', id: 'grp-main' }),
        group(['focused-side-a', 'files', 'focused-side-b'], { active: 'focused-side-b', id: 'grp-side' })
      ])
    )
    let closeCompletion: Promise<void> | undefined
    const closeFiles = vi.fn(() => setTreePaneHidden('files', true))

    let finishClose = () => {}
    registerPaneCloser('files', closeFiles)
    registerPaneCloser(
      'focused-side-b',
      () => {
        closeCompletion = new Promise<void>(resolve => {
          finishClose = () => {
            dismissTreePane('focused-side-b')
            resolve()
          }
        })

        return closeCompletion
      }
    )
    act(() => setTreePaneHidden('files', true))
    render(<LayoutTreeRoot />)

    try {
      const focusedSide = tabControl('focused-side-b')!
      focusedSide.focus()
      noteActiveTreeGroup('grp-side')
      expect(tabControl('workspace')?.getAttribute('aria-selected')).toBe('true')
      expect(tabControl('files')).toBeNull()
      act(() => {
        expect(closeActiveTab()).toBe(true)
      })

      await act(async () => {
        finishClose()
        await closeCompletion
      })

      await waitFor(() => {
        expect(findGroup($layoutTree.get()!, 'grp-side')?.active).toBe('files')
        expect(tabControl('focused-side-b')).toBeNull()
        expect(window.document.activeElement).toBe(tabControl('focused-side-a'))
      })

      act(() => $layoutEditMode.set(true))
      await waitFor(() => expect(tabControl('files')?.getAttribute('aria-selected')).toBe('true'))

      act(() => {
        expect(closeActiveTab()).toBe(true)
      })
      await waitFor(() => {
        expect(closeFiles).toHaveBeenCalledTimes(1)
        expect(findGroup($layoutTree.get()!, 'grp-side')?.panes).toEqual(['focused-side-a', 'files'])
      })

      act(() => $layoutEditMode.set(false))
      await waitFor(() => expect(window.document.activeElement).toBe(tabControl('focused-side-a')))

      act(() => {
        expect(closeActiveTab()).toBe(true)
      })
      await waitFor(() => {
        expect(closeFiles).toHaveBeenCalledTimes(1)
        expect(tabControl('focused-side-a')).toBeNull()
        expect(findGroup($layoutTree.get()!, 'grp-side')?.panes).toEqual(['files'])
      })
    } finally {
      act(() => registerPaneCloser('files'))
      act(() => registerPaneCloser('focused-side-b'))
    }
  })

  it('uses roving tabs and restores focus to the active tab after keyboard close', async () => {
    declareDefaultTree(
      split('column', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'logs'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    const view = render(<TreeGroup node={zoneAt(1)} parentAxis="column" />)
    const terminal = tabControl('terminal')!
    const logs = tabControl('logs')!

    expect(terminal.getAttribute('tabindex')).toBe('0')
    expect(logs.getAttribute('tabindex')).toBe('-1')

    terminal.focus()
    fireEvent.keyDown(terminal, { key: 'ArrowRight' })
    expect(window.document.activeElement).toBe(logs)

    view.rerender(<TreeGroup node={zoneAt(1)} parentAxis="column" />)
    expect(tabControl('logs')?.getAttribute('aria-selected')).toBe('true')

    const logsForLeft = tabControl('logs')!
    logsForLeft.focus()
    fireEvent.keyDown(logsForLeft, { key: 'ArrowLeft' })
    expect(window.document.activeElement).toBe(terminal)

    view.rerender(<TreeGroup node={zoneAt(1)} parentAxis="column" />)
    expect(tabControl('terminal')?.getAttribute('aria-selected')).toBe('true')

    const terminalForEnd = tabControl('terminal')!
    terminalForEnd.focus()
    fireEvent.keyDown(terminalForEnd, { key: 'End' })
    view.rerender(<TreeGroup node={zoneAt(1)} parentAxis="column" />)
    expect(tabControl('logs')?.getAttribute('aria-selected')).toBe('true')

    const logsForHome = tabControl('logs')!
    logsForHome.focus()
    fireEvent.keyDown(logsForHome, { key: 'Home' })
    view.rerender(<TreeGroup node={zoneAt(1)} parentAxis="column" />)
    expect(tabControl('terminal')?.getAttribute('aria-selected')).toBe('true')

    const logsForSpace = tabControl('logs')!
    logsForSpace.focus()
    fireEvent.keyDown(logsForSpace, { key: ' ' })
    view.rerender(<TreeGroup node={zoneAt(1)} parentAxis="column" />)
    expect(tabControl('logs')?.getAttribute('aria-selected')).toBe('true')

    const logsForEnter = tabControl('logs')!
    logsForEnter.focus()
    fireEvent.keyDown(logsForEnter, { key: 'Enter' })
    view.rerender(<TreeGroup node={zoneAt(1)} parentAxis="column" />)
    expect(tabControl('logs')?.getAttribute('aria-selected')).toBe('true')

    const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="terminal"] [data-pane-tab-close="true"]')!
    act(() => close.focus())
    fireEvent.click(close)
    view.rerender(<TreeGroup node={zoneAt(1)} parentAxis="column" />)

    await waitFor(() => expect(window.document.activeElement).toBe(tabControl('logs')))
  })

  it('keeps close-button focus recovery pending until a registered closer confirms', async () => {
    let confirmClose: (() => void) | undefined
    registerPaneCloser(
      'delayed-close-test',
      () =>
        new Promise<void>(resolve => {
          confirmClose = () => {
            dismissTreePane('delayed-close-test')
            resolve()
          }
        })
    )
    declareDefaultTree(
      split('column', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'delayed-close-test'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    const view = render(<TreeGroup node={zoneAt(1)} parentAxis="column" />)
    const closeSelector = '[data-tree-tab="delayed-close-test"] [data-pane-tab-close="true"]'
    await waitFor(() => expect(window.document.querySelector(closeSelector)).toBeTruthy())
    const close = window.document.querySelector<HTMLButtonElement>(closeSelector)!
    act(() => close.focus())
    fireEvent.click(close)
    view.rerender(<TreeGroup node={zoneAt(1)} parentAxis="column" />)

    expect(tabControl('delayed-close-test')).toBeTruthy()
    expect(confirmClose).toBeTypeOf('function')

    act(() => confirmClose?.())
    view.rerender(<TreeGroup node={zoneAt(1)} parentAxis="column" />)

    await waitFor(() => expect(window.document.activeElement).toBe(tabControl('terminal')))
    act(() => registerPaneCloser('delayed-close-test'))
  })

  it('keeps context-menu close recovery pending until a registered closer confirms', async () => {
    let confirmClose: (() => void) | undefined
    registerPaneCloser(
      'delayed-close-test',
      () =>
        new Promise<void>(resolve => {
          confirmClose = () => {
            dismissTreePane('delayed-close-test')
            resolve()
          }
        })
    )
    declareDefaultTree(
      split('column', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'delayed-close-test'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    render(<LayoutTreeRoot />)

    try {
      const tab = window.document.querySelector<HTMLElement>('[data-tree-tab="delayed-close-test"]')!

      fireEvent.pointerDown(tab, { button: 2, pointerType: 'mouse' })
      fireEvent.contextMenu(tab, { button: 2 })
      fireEvent.click(await screen.findByRole('menuitem', { name: /^close$/i }))

      expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'delayed-close-test', status: 'pending' })
      expect(confirmClose).toBeTypeOf('function')

      act(() => confirmClose?.())

      await waitFor(() => expect(window.document.activeElement).toBe(tabControl('terminal')))
    } finally {
      act(() => registerPaneCloser('delayed-close-test'))
    }
  })

  it('waits for the real busy-session confirmation before recovering close focus', async () => {
    $sessionStates.set({ 'busy-runtime': busySessionState })
    $sessionTiles.set([{ runtimeId: 'busy-runtime', storedSessionId: 'busy-session' }])
    declareDefaultTree(
      split('column', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'busy-session-pane'], { active: 'terminal', id: 'grp-tools' })
      ])
    )

    let completion: Promise<void> | undefined
    registerPaneCloser('busy-session-pane', () => (completion = requestCloseSessionTile('busy-session')))

    const stopMirror = $sessionTiles.listen(tiles => {
      if (!tiles.some(tile => tile.storedSessionId === 'busy-session')) {
        dismissTreePane('busy-session-pane')
      }
    })

    render(
      <>
        <LayoutTreeRoot />
        <SessionTileCloseConfirm />
      </>
    )

    try {
      const close = window.document.querySelector<HTMLButtonElement>(
        '[data-tree-tab="busy-session-pane"] [data-pane-tab-close="true"]'
      )!

      act(() => close.focus())
      act(() => fireEvent.click(close))

      expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'busy-session-pane', status: 'pending' })
      expect(await screen.findByRole('dialog', { name: 'Close running tab?' })).toBeTruthy()

      act(() => fireEvent.click(screen.getByRole('button', { name: 'Close tab' })))

      await waitFor(() => expect(tabControl('busy-session-pane')).toBeNull())
      expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'busy-session-pane', status: 'pending' })

      await act(async () => {
        await completion!
      })

      await waitFor(() => expect(window.document.activeElement).toBe(tabControl('terminal')))
    } finally {
      act(() => {
        stopMirror()
        registerPaneCloser('busy-session-pane')
      })
    }
  })

  it('does not let a global close supersede a pending busy close', async () => {
    $sessionStates.set({ 'busy-runtime': busySessionState })
    $sessionTiles.set([{ runtimeId: 'busy-runtime', storedSessionId: 'busy-session' }])
    declareDefaultTree(group(['workspace', 'busy-session-pane', 'delayed-close-test'], { active: 'busy-session-pane', id: 'grp-main' }))
    let completion: Promise<void> | undefined
    registerPaneCloser('busy-session-pane', () => (completion = requestCloseSessionTile('busy-session')))

    const stopMirror = $sessionTiles.listen(tiles => {
      if (!tiles.some(tile => tile.storedSessionId === 'busy-session')) {
        dismissTreePane('busy-session-pane')
      }
    })

    render(
      <>
        <LayoutTreeRoot />
        <SessionTileCloseConfirm />
      </>
    )

    try {
      const close = window.document.querySelector<HTMLButtonElement>(
        '[data-tree-tab="busy-session-pane"] [data-pane-tab-close="true"]'
      )!

      act(() => fireEvent.click(close))
      expect(await screen.findByRole('dialog', { name: 'Close running tab?' })).toBeTruthy()
      expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'busy-session-pane', status: 'pending' })

      act(() => {
        noteHoveredTreeGroup('grp-main')
        expect(cycleTreeTabInFocusedZone(1)).toBe('delayed-close-test')
      })

      let closed = true
      act(() => {
        closed = closeActiveTab()
      })

      expect(closed).toBe(false)
      expect(tabControl('delayed-close-test')).not.toBeNull()
      expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'busy-session-pane', status: 'pending' })

      act(() => fireEvent.click(screen.getByRole('button', { name: 'Close tab' })))

      await waitFor(() => expect(tabControl('busy-session-pane')).toBeNull())
      await act(async () => {
        await completion!
      })
      await waitFor(() => expect(window.document.activeElement).toBe(tabControl('delayed-close-test')))
    } finally {
      act(() => {
        noteHoveredTreeGroup(null)
        stopMirror()
        registerPaneCloser('busy-session-pane')
      })
    }
  })

  it('moves focus from a vertical rail tab to its restored horizontal tab', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'logs'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    setTreeGroupMinimized('grp-tools', true)
    render(<LayoutTreeRoot />)

    const terminal = tabControl('terminal')!
    terminal.focus()
    fireEvent.keyDown(terminal, { key: 'ArrowDown' })

    await waitFor(() => {
      const logs = tabControl('logs')

      expect(logs?.getAttribute('aria-selected')).toBe('true')
      expect(window.document.activeElement).toBe(logs)
    })
  })

  it('wraps vertical rail navigation and ignores modified arrow keys', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'logs'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    setTreeGroupMinimized('grp-tools', true)
    render(<LayoutTreeRoot />)

    const terminal = tabControl('terminal')!
    terminal.focus()
    fireEvent.keyDown(terminal, { key: 'ArrowDown', metaKey: true })
    expect(window.document.activeElement).toBe(terminal)
    expect(findGroup($layoutTree.get()!, 'grp-tools')?.active).toBe('terminal')

    fireEvent.keyDown(terminal, { key: 'ArrowUp' })

    await waitFor(() => {
      const logs = tabControl('logs')

      expect(logs?.getAttribute('aria-selected')).toBe('true')
      expect(window.document.activeElement).toBe(logs)
    })
  })

  it('keeps keyboard focus when Space restores a vertical rail tab', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'logs'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    setTreeGroupMinimized('grp-tools', true)
    render(<LayoutTreeRoot />)

    const logs = tabControl('logs')!
    logs.focus()
    fireEvent.keyDown(logs, { key: ' ' })

    await waitFor(() => {
      const restoredLogs = tabControl('logs')

      expect(restoredLogs?.getAttribute('aria-selected')).toBe('true')
      expect(window.document.activeElement).toBe(restoredLogs)
    })
  })

  it('keeps focus when a primary click restores a focused vertical rail tab', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'logs'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    setTreeGroupMinimized('grp-tools', true)
    render(<LayoutTreeRoot />)

    const terminal = tabControl('terminal')!
    act(() => {
      terminal.focus()
      fireEvent.click(terminal)
    })

    await waitFor(() => {
      const restoredTerminal = tabControl('terminal')

      expect(restoredTerminal?.getAttribute('aria-selected')).toBe('true')
      expect(window.document.activeElement).toBe(restoredTerminal)
    })
  })

  it('keeps focus when the vertical rail background restores its active tab', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'logs'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    setTreeGroupMinimized('grp-tools', true)
    render(<LayoutTreeRoot />)

    const terminal = tabControl('terminal')!
    const rail = terminal.closest('[role="tablist"]')?.parentElement

    expect(rail).not.toBeNull()
    act(() => {
      terminal.focus()
      fireEvent.click(rail!)
    })

    await waitFor(() => {
      const restoredTerminal = tabControl('terminal')

      expect(restoredTerminal?.getAttribute('aria-selected')).toBe('true')
      expect(window.document.activeElement).toBe(restoredTerminal)
    })
  })

  it('keeps focus when the vertical rail context menu restores its active tab', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'logs'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    setTreeGroupMinimized('grp-tools', true)
    render(<LayoutTreeRoot />)

    const terminal = tabControl('terminal')!
    act(() => {
      terminal.focus()
      fireEvent.contextMenu(terminal, { button: 2 })
    })
    fireEvent.click(await screen.findByRole('menuitem', { name: /^restore$/i }))

    await waitFor(() => {
      const restoredTerminal = tabControl('terminal')

      expect(restoredTerminal?.getAttribute('aria-selected')).toBe('true')
      expect(window.document.activeElement).toBe(restoredTerminal)
    })
  })

  it('keeps focus when a primary click restores a focused horizontal minimized tab', async () => {
    declareDefaultTree(
      split('column', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'logs'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    setTreeGroupMinimized('grp-tools', true)
    render(<LayoutTreeRoot />)

    const terminal = tabControl('terminal')!
    act(() => {
      terminal.focus()
      fireEvent.click(terminal)
    })

    await waitFor(() => {
      const restoredTerminal = tabControl('terminal')

      expect(restoredTerminal?.getAttribute('aria-selected')).toBe('true')
      expect(window.document.activeElement).toBe(restoredTerminal)
    })
  })

  it('keeps focus when the horizontal minimized tab context menu restores its active tab', async () => {
    declareDefaultTree(
      split('column', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'logs'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    setTreeGroupMinimized('grp-tools', true)
    render(<LayoutTreeRoot />)

    const terminal = tabControl('terminal')!
    act(() => {
      terminal.focus()
      fireEvent.contextMenu(terminal, { button: 2 })
    })
    fireEvent.click(await screen.findByRole('menuitem', { name: /^restore$/i }))

    await waitFor(() => {
      const restoredTerminal = tabControl('terminal')

      expect(restoredTerminal?.getAttribute('aria-selected')).toBe('true')
      expect(window.document.activeElement).toBe(restoredTerminal)
    })
  })

  it('moves focus to the surviving workspace composer when closing a lone tab', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    render(<LayoutTreeRoot />)

    const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="terminal"] [data-pane-tab-close="true"]')!
    act(() => close.focus())
    act(() => fireEvent.click(close))

    await waitFor(() => {
      expect(findGroup($layoutTree.get()!, 'grp-tools')).toBeNull()
      expect(window.document.activeElement).toBe(window.document.querySelector('[data-slot="composer-rich-input"]'))
    })
  })

  it('recovers focus after a registered closer removes a lone tab group', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['delayed-close-test'], { active: 'delayed-close-test', id: 'grp-tools' })
      ])
    )

    let finishClose = () => {}
    act(() => {
      registerPaneCloser(
        'delayed-close-test',
        () =>
          new Promise<void>(resolve => {
            finishClose = () => {
              dismissTreePane('delayed-close-test')
              resolve()
            }
          })
      )
    })
    render(<LayoutTreeRoot />)

    try {
      const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="delayed-close-test"] [data-pane-tab-close="true"]')!
      act(() => close.focus())
      act(() => fireEvent.click(close))

      expect(window.document.activeElement).toBe(close)

      act(() => finishClose())

      await waitFor(() => {
        expect(findGroup($layoutTree.get()!, 'grp-tools')).toBeNull()
        expect(window.document.activeElement).toBe(window.document.querySelector('[data-slot="composer-rich-input"]'))
      })
    } finally {
      act(() => registerPaneCloser('delayed-close-test'))
    }
  })

  it('keeps recovery pending for an inactive tab while its registered close is deferred', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'delayed-close-test'], { active: 'terminal', id: 'grp-tools' })
      ])
    )

    let finishClose = () => {}
    act(() => {
      registerPaneCloser('delayed-close-test', () =>
        new Promise<void>(resolve => {
          finishClose = () => {
            dismissTreePane('delayed-close-test')
            resolve()
          }
        })
      )
    })
    render(<LayoutTreeRoot />)

    try {
      const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="delayed-close-test"] [data-pane-tab-close="true"]')!
      act(() => close.focus())
      act(() => fireEvent.click(close))

      await act(async () => {
        await new Promise(resolve => window.setTimeout(resolve, 30))
      })
      expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'delayed-close-test', kind: 'close' })

      act(() => finishClose())

      await waitFor(() => {
        expect(tabControl('terminal')).toBeTruthy()
        expect(window.document.activeElement).toBe(tabControl('terminal'))
      })
    } finally {
      act(() => registerPaneCloser('delayed-close-test'))
    }
  })

  it('recovers focus after a synchronous registered close removes an inactive tab', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'delayed-close-test'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    act(() => registerPaneCloser('delayed-close-test', () => dismissTreePane('delayed-close-test')))
    render(<LayoutTreeRoot />)

    try {
      const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="delayed-close-test"] [data-pane-tab-close="true"]')!
      act(() => close.focus())
      act(() => fireEvent.click(close))

      await waitFor(() => {
        expect(tabControl('delayed-close-test')).toBeNull()
        expect(window.document.activeElement).toBe(tabControl('terminal'))
      })
    } finally {
      act(() => registerPaneCloser('delayed-close-test'))
    }
  })

  it('moves pointer-close focus from the composer to the surviving selected tab', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['terminal', 'delayed-close-test'], { active: 'terminal', id: 'grp-tools' })
      ])
    )
    act(() => registerPaneCloser('delayed-close-test', () => dismissTreePane('delayed-close-test')))
    render(<LayoutTreeRoot />)

    try {
      const composer = window.document.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')!

      const close = window.document.querySelector<HTMLButtonElement>(
        '[data-tree-tab="delayed-close-test"] [data-pane-tab-close="true"]'
      )!

      act(() => composer.focus())
      expect(window.document.activeElement).toBe(composer)

      act(() => fireEvent.pointerDown(close, { button: 0, pointerType: 'mouse' }))
      expect(window.document.activeElement).toBe(close)
      act(() => fireEvent.click(close, { button: 0 }))

      await waitFor(() => {
        expect(tabControl('delayed-close-test')).toBeNull()
        expect(window.document.activeElement).toBe(tabControl('terminal'))
      })
    } finally {
      act(() => registerPaneCloser('delayed-close-test'))
    }
  })

  it('recovers focus into the workspace composer after a middle-click close removes the focused tab', async () => {
    declareDefaultTree(group(['workspace', 'delayed-close-test'], { active: 'delayed-close-test', id: 'grp-main' }))
    act(() => registerPaneCloser('delayed-close-test', () => dismissTreePane('delayed-close-test')))
    render(<LayoutTreeRoot />)

    try {
      const tab = tabControl('delayed-close-test')!
      act(() => tab.focus())

      act(() => {
        fireEvent.pointerDown(tab, { button: 1, pointerType: 'mouse' })
        fireEvent.pointerUp(tab, { button: 1, pointerType: 'mouse' })
      })

      await waitFor(() => {
        expect(tabControl('delayed-close-test')).toBeNull()
        expect(window.document.activeElement).toBe(window.document.querySelector('[data-slot="composer-rich-input"]'))
      })
    } finally {
      act(() => registerPaneCloser('delayed-close-test'))
    }
  })

  it('cancels pending focus recovery when a registered close is rejected', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['delayed-close-test'], { active: 'delayed-close-test', id: 'grp-tools' })
      ])
    )

    let rejectClose = () => {}
    act(() => {
      registerPaneCloser('delayed-close-test', () => {
        const result = new Promise<void>((_resolve, reject) => {
          rejectClose = () => reject(new Error('close canceled'))
        })

        void result.catch(() => undefined)

        return result
      })
    })
    render(<LayoutTreeRoot />)

    try {
      const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="delayed-close-test"] [data-pane-tab-close="true"]')!
      act(() => close.focus())
      act(() => fireEvent.click(close))
      expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'delayed-close-test', kind: 'close' })

      act(() => rejectClose())

      await waitFor(() => expect($treeFocusRequest.get()).toBeNull())
      expect(window.document.activeElement).toBe(close)
    } finally {
      act(() => registerPaneCloser('delayed-close-test'))
    }
  })

  it('restores the source tab when a rejected close leaves focus on the document body', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['delayed-close-test'], { active: 'delayed-close-test', id: 'grp-tools' })
      ])
    )

    let rejectClose = () => {}
    act(() => {
      registerPaneCloser('delayed-close-test', () => {
        const result = new Promise<void>((_resolve, reject) => {
          rejectClose = () => reject(new Error('close canceled'))
        })

        void result.catch(() => undefined)

        return result
      })
    })
    render(<LayoutTreeRoot />)

    try {
      const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="delayed-close-test"] [data-pane-tab-close="true"]')!
      act(() => close.focus())
      act(() => fireEvent.click(close))
      expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'delayed-close-test', kind: 'close' })

      act(() => {
        close.blur()
        rejectClose()
      })

      await waitFor(() => {
        expect($treeFocusRequest.get()).toBeNull()
        expect(window.document.activeElement).toBe(tabControl('delayed-close-test'))
      })
    } finally {
      act(() => registerPaneCloser('delayed-close-test'))
    }
  })

  it('clears recovery when a synchronous closer leaves its close control visible', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['delayed-close-test'], { active: 'delayed-close-test', id: 'grp-tools' })
      ])
    )
    act(() => registerPaneCloser('delayed-close-test', () => undefined))
    render(<LayoutTreeRoot />)

    try {
      const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="delayed-close-test"] [data-pane-tab-close="true"]')!
      act(() => close.focus())
      act(() => fireEvent.click(close))

      await waitFor(() => expect($treeFocusRequest.get()).toBeNull())
      expect(window.document.activeElement).toBe(close)
    } finally {
      act(() => registerPaneCloser('delayed-close-test'))
    }
  })

  it('clears recovery when a deferred closer settles without removing its close control', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['delayed-close-test'], { active: 'delayed-close-test', id: 'grp-tools' })
      ])
    )
    act(() => registerPaneCloser('delayed-close-test', () => Promise.resolve()))
    render(<LayoutTreeRoot />)

    try {
      const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="delayed-close-test"] [data-pane-tab-close="true"]')!
      act(() => close.focus())
      act(() => fireEvent.click(close))

      await waitFor(() => expect($treeFocusRequest.get()).toBeNull())
      expect(window.document.activeElement).toBe(close)
    } finally {
      act(() => registerPaneCloser('delayed-close-test'))
    }
  })

  it('clears a pending deferred-close request when its layout root unmounts', () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['delayed-close-test'], { active: 'delayed-close-test', id: 'grp-tools' })
      ])
    )
    act(() => registerPaneCloser('delayed-close-test', () => new Promise<void>(() => undefined)))
    const view = render(<LayoutTreeRoot />)

    try {
      const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="delayed-close-test"] [data-pane-tab-close="true"]')!
      act(() => close.focus())
      act(() => fireEvent.click(close))
      expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'delayed-close-test', kind: 'close' })

      view.unmount()

      expect($treeFocusRequest.get()).toBeNull()
    } finally {
      act(() => registerPaneCloser('delayed-close-test'))
    }
  })

  it('recovers focus after a registered closer hides a lone tab group', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['delayed-close-test'], { active: 'delayed-close-test', id: 'grp-tools' })
      ])
    )
    act(() => {
      registerPaneCloser('delayed-close-test', () => setTreePaneHidden('delayed-close-test', true))
    })
    render(<LayoutTreeRoot />)

    try {
      const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="delayed-close-test"] [data-pane-tab-close="true"]')!
      act(() => close.focus())
      act(() => fireEvent.click(close))

      await waitFor(() => {
        expect($hiddenTreePanes.get().has('delayed-close-test')).toBe(true)
        expect(window.document.activeElement).toBe(window.document.querySelector('[data-slot="composer-rich-input"]'))
      })
    } finally {
      act(() => registerPaneCloser('delayed-close-test'))
    }
  })

  it('recovers focus after a registered closer collapses the source side', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['side-close-test', 'side-close-sibling'], { active: 'side-close-test', id: 'grp-side' })
      ])
    )
    act(() => {
      registerPaneCloser('side-close-test', () => setTreeSideCollapsed('right', true))
    })
    render(<LayoutTreeRoot />)

    try {
      const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="side-close-test"] [data-pane-tab-close="true"]')!
      act(() => close.focus())
      act(() => fireEvent.click(close))

      await waitFor(() => {
        expect($collapsedTreeSides.get().has('right')).toBe(true)
        expect(window.document.activeElement).toBe(window.document.querySelector('[data-slot="composer-rich-input"]'))
      })
    } finally {
      act(() => registerPaneCloser('side-close-test'))
    }
  })

  it('skips a selected tab hidden in a collapsed side when recovering focus', async () => {
    declareDefaultTree(
      split('column', [
        split('row', [
          group(['workspace'], { active: 'workspace', id: 'grp-main' }),
          group(['side-close-test', 'side-close-sibling'], { active: 'side-close-test', id: 'grp-side' })
        ]),
        group(['delayed-close-test'], { active: 'delayed-close-test', id: 'grp-tools' })
      ])
    )
    act(() => {
      registerPaneCloser('delayed-close-test', () => dismissTreePane('delayed-close-test'))
      setTreeSideCollapsed('right', true)
    })
    render(<LayoutTreeRoot />)

    try {
      const hiddenSelected = tabControl('side-close-test')!
      const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="delayed-close-test"] [data-pane-tab-close="true"]')!
      expect(hiddenSelected.getAttribute('aria-selected')).toBe('true')
      act(() => close.focus())
      act(() => fireEvent.click(close))

      await waitFor(() => {
        expect(window.document.activeElement).toBe(window.document.querySelector('[data-slot="composer-rich-input"]'))
      })
    } finally {
      act(() => registerPaneCloser('delayed-close-test'))
    }
  })

  it('moves focus to an application fallback when a close hides the remaining lone tab', async () => {
    declareDefaultTree(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group(['plain-a', 'plain-b'], { active: 'plain-a', id: 'grp-tools' })
      ])
    )
    act(() => {
      registerPaneCloser('plain-a', () => dismissTreePane('plain-a'))
      registerPaneCloser('plain-b', () => dismissTreePane('plain-b'))
    })
    render(<LayoutTreeRoot />)

    try {
      const close = window.document.querySelector<HTMLButtonElement>('[data-tree-tab="plain-a"] [data-pane-tab-close="true"]')!
      act(() => close.focus())
      act(() => fireEvent.click(close))

      await waitFor(() => {
        expect(findGroup($layoutTree.get()!, 'grp-tools')?.panes).toEqual(['plain-b'])
        expect(tabControl('plain-b')).toBeNull()
        expect(window.document.activeElement).toBe(window.document.querySelector('[data-slot="composer-rich-input"]'))
      })
    } finally {
      act(() => {
        registerPaneCloser('plain-a')
        registerPaneCloser('plain-b')
      })
    }
  })
})
