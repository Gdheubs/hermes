import { atom } from 'nanostores'

import { hasTreeFocusRecovery } from '@/components/pane-shell/tree/tree-focus'

export const TERMINAL_RAIL_FOCUS_HANDOFF_ATTR = 'data-terminal-rail-focus-handoff'
export const $terminalRailFocusHandoff = atom(false)

interface FocusableTerminal {
  focus: () => void
}

/** A close/roving handoff intentionally leaves focus on the selected rail tab.
 * Late xterm initialization must not claim it back. */
function terminalRailOwnsFocus(): boolean {
  if (typeof document === 'undefined') {
    return false
  }

  const active = document.activeElement

  return (
    active instanceof HTMLElement &&
    active.matches(`[${TERMINAL_RAIL_FOCUS_HANDOFF_ATTR}][data-terminal-rail-tab][aria-selected="true"]`)
  )
}

/** Whether a terminal-rail tab initiated the current close command. */
export function terminalRailTabHasFocus(): boolean {
  if (typeof document === 'undefined') {
    return false
  }

  return document.activeElement instanceof HTMLElement && document.activeElement.matches('[data-terminal-rail-tab]')
}

/** Request post-commit focus for the selected terminal rail tab. */
export function requestTerminalRailFocusHandoff(): void {
  $terminalRailFocusHandoff.set(true)
}

export function clearTerminalRailFocusHandoff(): void {
  $terminalRailFocusHandoff.set(false)
}

export function focusTerminalUnlessRailOwnsFocus(terminal: FocusableTerminal): boolean {
  if (hasTreeFocusRecovery() || terminalRailOwnsFocus()) {
    return false
  }

  terminal.focus()

  return true
}
