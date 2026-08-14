import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  $treeFocusRequest,
  clearTreeFocusRequest,
  requestTreeFocusAfterClose,
  requestTreeFocusAfterRestore,
  settleTreeFocusAfterClose
} from '@/components/pane-shell/tree/tree-focus'

import { focusTerminalUnlessRailOwnsFocus, TERMINAL_RAIL_FOCUS_HANDOFF_ATTR } from './focus-handoff'

afterEach(() => {
  $treeFocusRequest.set(null)
  document.body.replaceChildren()
})

describe('focusTerminalUnlessRailOwnsFocus', () => {
  it('preserves a selected terminal rail tab during a close handoff', () => {
    const terminal = { focus: vi.fn() }
    const tab = document.createElement('button')
    tab.setAttribute('aria-selected', 'true')
    tab.setAttribute('data-terminal-rail-tab', 'terminal-1')
    tab.setAttribute(TERMINAL_RAIL_FOCUS_HANDOFF_ATTR, '')
    document.body.append(tab)
    tab.focus()

    expect(focusTerminalUnlessRailOwnsFocus(terminal)).toBe(false)
    expect(terminal.focus).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(tab)
  })

  it('focuses the terminal when the rail does not own a handoff', () => {
    const terminal = { focus: vi.fn() }

    expect(focusTerminalUnlessRailOwnsFocus(terminal)).toBe(true)
    expect(terminal.focus).toHaveBeenCalledOnce()
  })

  it('does not steal a pending or settled tree-close recovery', () => {
    const terminal = { focus: vi.fn() }
    const request = requestTreeFocusAfterClose('plugin-pane')

    expect(focusTerminalUnlessRailOwnsFocus(terminal)).toBe(false)
    expect(terminal.focus).not.toHaveBeenCalled()

    settleTreeFocusAfterClose(request)
    expect(focusTerminalUnlessRailOwnsFocus(terminal)).toBe(false)
    expect(terminal.focus).not.toHaveBeenCalled()

    clearTreeFocusRequest(request)
    expect(focusTerminalUnlessRailOwnsFocus(terminal)).toBe(true)
    expect(terminal.focus).toHaveBeenCalledOnce()
  })

  it('does not steal a tree restore handoff before the restored tab receives focus', () => {
    const terminal = { focus: vi.fn() }

    requestTreeFocusAfterRestore('grp-tools', 'terminal')
    const request = $treeFocusRequest.get()!

    expect(focusTerminalUnlessRailOwnsFocus(terminal)).toBe(false)
    expect(terminal.focus).not.toHaveBeenCalled()

    clearTreeFocusRequest(request)
    expect(focusTerminalUnlessRailOwnsFocus(terminal)).toBe(true)
    expect(terminal.focus).toHaveBeenCalledOnce()
  })
})
