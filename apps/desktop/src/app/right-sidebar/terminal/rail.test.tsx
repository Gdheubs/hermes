import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { closeActiveTab } from '@/app/chat/close-tab'
import { $treeFocusRequest, requestTreeFocusAfterClose } from '@/components/pane-shell/tree/tree-focus'
import { $bindings } from '@/store/keybinds'

import { TerminalRail } from './rail'
import { $activeTerminalId, $terminals, closeFocusedTerminal } from './terminals'
import { TerminalWorkspace } from './workspace'

vi.mock('./instance', () => ({
  AgentTerminalInstance: ({ id }: { id: string }) => (
    <button aria-label={`Agent terminal ${id}`} data-agent-terminal={id} data-terminal="" type="button" />
  ),
  TerminalInstance: ({ id }: { id: string }) => (
    <button aria-label={`User terminal ${id}`} data-terminal="" data-user-terminal={id} type="button" />
  )
}))

describe('TerminalRail', () => {
  beforeEach(() => {
    $terminals.set([{ auto: true, cwd: 'C:\\repo', id: 'term-1', kind: 'user', title: 'PowerShell' }])
    $activeTerminalId.set('term-1')
    $bindings.set({ ...$bindings.get(), 'view.showTerminal': ['ctrl+`'] })
  })

  afterEach(() => {
    cleanup()
    $treeFocusRequest.set(null)
    $terminals.set([])
    $activeTerminalId.set(null)
  })

  it('keeps a hotkey label inline inside the portaled tooltip decoration', async () => {
    const view = render(<TerminalRail />)

    fireEvent.pointerMove(screen.getByRole('tab', { name: '1. PowerShell' }), { pointerType: 'mouse' })
    await screen.findByRole('tooltip')

    const content = document.querySelector<HTMLElement>('[data-slot="tooltip-content"]')
    const label = content?.firstElementChild?.firstElementChild

    expect(content).not.toBeNull()
    expect(view.container.contains(content)).toBe(false)
    expect(label?.classList.contains('inline-flex')).toBe(true)
    expect(label?.classList.contains('flex')).toBe(false)
  })

  it('⌘-click closes the tab; a plain click selects it', () => {
    $terminals.set([...$terminals.get(), { auto: true, cwd: 'C:\\repo', id: 'term-2', kind: 'user', title: 'zsh' }])

    render(<TerminalRail />)

    fireEvent.click(screen.getByRole('tab', { name: '2. zsh' }), { metaKey: true })
    expect($terminals.get().map(term => term.id)).toEqual(['term-1'])

    fireEvent.click(screen.getByRole('tab', { name: '1. PowerShell' }))
    expect($activeTerminalId.get()).toBe('term-1')
    expect($terminals.get()).toHaveLength(1)
  })

  it('recovers focus to the selected terminal tab after closing the focused tab', async () => {
    $terminals.set([
      { auto: true, cwd: 'C:\\repo', id: 'term-1', kind: 'user', title: 'PowerShell' },
      { auto: true, cwd: 'C:\\repo', id: 'term-2', kind: 'user', title: 'zsh' }
    ])
    $activeTerminalId.set('term-2')

    render(<TerminalRail />)

    const closingTab = screen.getByRole('tab', { name: '2. zsh' })
    act(() => {
      closingTab.focus()
      fireEvent.click(closingTab, { metaKey: true })
    })

    expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'terminal', status: 'settled' })
    const selectedTab = screen.getByRole('tab', { name: '1. PowerShell' })

    await waitFor(() => expect(window.document.activeElement).toBe(selectedTab))
    expect(selectedTab.getAttribute('data-terminal-rail-focus-handoff')).toBe('')
  })

  it('routes ⌘W from a selected rail tab to the active terminal tab', () => {
    $terminals.set([
      { auto: true, cwd: 'C:\\repo', id: 'term-1', kind: 'user', title: 'PowerShell' },
      { auto: true, cwd: 'C:\\repo', id: 'term-2', kind: 'user', title: 'zsh' }
    ])
    $activeTerminalId.set('term-2')
    render(<TerminalRail />)

    act(() => {
      screen.getByRole('tab', { name: '2. zsh' }).focus()

      expect(closeActiveTab()).toBe(true)
    })
    expect($terminals.get().map(term => term.id)).toEqual(['term-1'])
  })

  it('routes ⌘W to the focused terminal instance and keeps the survivor in the close scope', async () => {
    $terminals.set([
      { auto: true, cwd: 'C:\\repo', id: 'term-1', kind: 'user', title: 'PowerShell' },
      { auto: true, cwd: 'C:\\repo', id: 'term-2', kind: 'user', title: 'zsh' }
    ])
    $activeTerminalId.set('term-2')
    render(
      <>
        <TerminalRail />
        <TerminalWorkspace onAddSelectionToChat={() => undefined} />
      </>
    )

    const focusedTerminal = screen.getByRole('button', { name: 'User terminal term-2' })
    act(() => focusedTerminal.focus())

    // Selection can change while xterm retains DOM focus (for example when a
    // nested terminal group is activated from another surface).
    act(() => $activeTerminalId.set('term-1'))
    expect(window.document.activeElement).toBe(focusedTerminal)

    act(() => {
      expect(closeActiveTab()).toBe(true)
    })

    expect($terminals.get().map(term => term.id)).toEqual(['term-1'])
    const survivor = screen.getByRole('tab', { name: '1. PowerShell' })
    await waitFor(() => expect(window.document.activeElement).toBe(survivor))

    act(() => {
      expect(closeActiveTab()).toBe(true)
    })

    expect($terminals.get()).toEqual([])
  })

  it('declines a stale focused terminal owner without closing the active survivor', () => {
    const stalePanel = window.document.createElement('div')
    const staleInput = window.document.createElement('button')

    stalePanel.dataset.terminal = ''
    stalePanel.dataset.terminalId = 'removed-terminal'
    stalePanel.append(staleInput)
    window.document.body.append(stalePanel)
    staleInput.focus()

    expect(closeFocusedTerminal()).toBe(false)
    expect($terminals.get().map(term => term.id)).toEqual(['term-1'])

    stalePanel.remove()
  })

  it('reports a stale focused terminal owner as an owned global-close no-op', () => {
    const stalePanel = window.document.createElement('div')
    const staleInput = window.document.createElement('button')

    stalePanel.dataset.terminal = ''
    stalePanel.dataset.terminalId = 'removed-terminal'
    stalePanel.append(staleInput)
    window.document.body.append(stalePanel)
    staleInput.focus()

    expect(closeActiveTab()).toBe(false)
    expect($terminals.get().map(term => term.id)).toEqual(['term-1'])

    stalePanel.remove()
  })

  it('keeps rail focus when ⌘W closes a selected rail tab', async () => {
    $terminals.set([
      { auto: true, cwd: 'C:\\repo', id: 'term-1', kind: 'user', title: 'PowerShell' },
      { auto: true, cwd: 'C:\\repo', id: 'term-2', kind: 'user', title: 'zsh' }
    ])
    $activeTerminalId.set('term-2')
    render(<TerminalRail />)

    act(() => {
      screen.getByRole('tab', { name: '2. zsh' }).focus()

      expect(closeActiveTab()).toBe(true)
    })

    await waitFor(() => expect(window.document.activeElement).toBe(screen.getByRole('tab', { name: '1. PowerShell' })))
  })

  it('releases the terminal focus handoff when focus leaves the selected tab', async () => {
    $terminals.set([
      { auto: true, cwd: 'C:\\repo', id: 'term-1', kind: 'user', title: 'PowerShell' },
      { auto: true, cwd: 'C:\\repo', id: 'term-2', kind: 'user', title: 'zsh' }
    ])
    $activeTerminalId.set('term-2')

    render(<TerminalRail />)

    const closingTab = screen.getByRole('tab', { name: '2. zsh' })
    act(() => {
      closingTab.focus()
      fireEvent.click(closingTab, { metaKey: true })
    })

    const selectedTab = screen.getByRole('tab', { name: '1. PowerShell' })
    await waitFor(() => expect(selectedTab.getAttribute('data-terminal-rail-focus-handoff')).toBe(''))
    act(() => screen.getByRole('button', { name: 'New terminal' }).focus())

    await waitFor(() => expect(selectedTab.getAttribute('data-terminal-rail-focus-handoff')).toBeNull())
  })

  it('routes middle-click close through the shared focus lifecycle', () => {
    $terminals.set([...$terminals.get(), { auto: true, cwd: 'C:\\repo', id: 'term-2', kind: 'user', title: 'zsh' }])

    render(<TerminalRail />)

    const tab = screen.getByRole('tab', { name: '2. zsh' })
    act(() => {
      fireEvent.pointerDown(tab, { button: 1, pointerType: 'mouse' })
      fireEvent.mouseDown(tab, { button: 1 })
      fireEvent.pointerUp(tab, { button: 1, pointerType: 'mouse' })
    })

    expect($terminals.get().map(term => term.id)).toEqual(['term-1'])
    expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'terminal', status: 'settled' })
  })

  it('routes context-menu bulk close through the shared focus lifecycle', async () => {
    $terminals.set([...$terminals.get(), { auto: true, cwd: 'C:\\repo', id: 'term-2', kind: 'user', title: 'zsh' }])

    render(<TerminalRail />)

    const tab = screen.getByRole('tab', { name: '2. zsh' })
    act(() => {
      fireEvent.pointerDown(tab, { button: 2, pointerType: 'mouse' })
      fireEvent.contextMenu(tab, { button: 2 })
    })
    fireEvent.click(await screen.findByRole('menuitem', { name: /close all/i }))

    expect($terminals.get()).toEqual([])
    expect($treeFocusRequest.get()).toMatchObject({ closedPaneId: 'terminal', status: 'settled' })
  })

  it('does not close a terminal while another pane owns deferred focus recovery', () => {
    const pending = requestTreeFocusAfterClose('busy-session')
    $terminals.set([...$terminals.get(), { auto: true, cwd: 'C:\\repo', id: 'term-2', kind: 'user', title: 'zsh' }])

    render(<TerminalRail />)

    fireEvent.click(screen.getByRole('tab', { name: '2. zsh' }), { metaKey: true })

    expect($terminals.get().map(term => term.id)).toEqual(['term-1', 'term-2'])
    expect($treeFocusRequest.get()).toBe(pending)
  })

  it('uses vertical roving terminal tabs tied to matching tabpanels', async () => {
    $terminals.set([
      { auto: true, cwd: 'C:\\repo', id: 'term-1', kind: 'user', title: 'PowerShell' },
      { auto: true, cwd: 'C:\\repo', id: 'term-2', kind: 'user', title: 'zsh' }
    ])

    const view = render(
      <>
        <TerminalRail />
        <TerminalWorkspace onAddSelectionToChat={() => undefined} />
      </>
    )

    const tablist = screen.getByRole('tablist')
    const firstTab = screen.getByRole('tab', { name: '1. PowerShell' })
    const secondTab = screen.getByRole('tab', { name: '2. zsh' })
    const firstPanelId = firstTab.getAttribute('aria-controls')

    const firstPanel = Array.from(view.baseElement.querySelectorAll<HTMLElement>('[role="tabpanel"]')).find(
      panel => panel.id === firstPanelId
    )

    const secondPanel = Array.from(view.baseElement.querySelectorAll<HTMLElement>('[role="tabpanel"]')).find(
      panel => panel.id === secondTab.getAttribute('aria-controls')
    )

    expect(tablist.getAttribute('aria-orientation')).toBe('vertical')
    expect(firstTab.getAttribute('tabindex')).toBe('0')
    expect(secondTab.getAttribute('tabindex')).toBe('-1')
    expect(firstTab.id).not.toBe('')
    expect(firstPanel?.getAttribute('role')).toBe('tabpanel')
    expect(firstPanel?.getAttribute('aria-labelledby')).toBe(firstTab.id)
    expect(firstPanel?.getAttribute('aria-hidden')).toBe('false')
    expect(secondPanel?.getAttribute('aria-hidden')).toBe('true')

    act(() => {
      firstTab.focus()
      fireEvent.keyDown(firstTab, { key: 'ArrowDown' })
    })

    expect($activeTerminalId.get()).toBe('term-2')
    await waitFor(() => expect(window.document.activeElement).toBe(secondTab))
    expect(secondTab.getAttribute('tabindex')).toBe('0')
    expect(firstTab.getAttribute('tabindex')).toBe('-1')
    expect(firstPanel?.getAttribute('aria-hidden')).toBe('true')
    expect(secondPanel?.getAttribute('aria-hidden')).toBe('false')

    act(() => {
      fireEvent.keyDown(secondTab, { key: 'Home' })
    })

    await waitFor(() => expect(window.document.activeElement).toBe(firstTab))
    expect($activeTerminalId.get()).toBe('term-1')

    act(() => {
      fireEvent.keyDown(firstTab, { key: 'End' })
    })

    await waitFor(() => expect(window.document.activeElement).toBe(secondTab))
    expect($activeTerminalId.get()).toBe('term-2')
  })
})
