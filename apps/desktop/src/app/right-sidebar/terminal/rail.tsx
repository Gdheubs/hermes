import { useStore } from '@nanostores/react'
import { type KeyboardEvent, useEffect, useRef, useState } from 'react'

import { togglePaneVisible } from '@/components/pane-shell/tree/store'
import { Codicon } from '@/components/ui/codicon'
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger
} from '@/components/ui/context-menu'
import { Tip, TipHintLabel } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { formatCombo } from '@/lib/keybinds/combo'
import { isMetaClose, middleClickHandlers } from '@/lib/middle-click'
import { cn } from '@/lib/utils'
import { $bindings } from '@/store/keybinds'

import {
  $terminalRailFocusHandoff,
  clearTerminalRailFocusHandoff,
  TERMINAL_RAIL_FOCUS_HANDOFF_ATTR
} from './focus-handoff'
import { terminalPanelId, terminalTabId } from './tab-aria'
import {
  $activeTerminalId,
  $terminals,
  closeAllTerminalsWithFocusRecovery,
  closeOtherTerminalsWithFocusRecovery,
  closeTerminalWithFocusRecovery,
  createTerminal,
  selectTerminal,
  type TerminalEntry
} from './terminals'

const RAIL_ACTION =
  'grid size-6 place-items-center rounded text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring [-webkit-app-region:no-drag]'

/** Thin icon "bookmark" strip blended into the terminal surface, shown whenever a
 *  terminal exists. Each square is a tab (name + hotkey on hover); close via the
 *  shell's `exit`, middle-click, or the context menu. */
export function TerminalRail() {
  const { t } = useI18n()
  const terminals = useStore($terminals)
  const activeId = useStore($activeTerminalId)
  const bindings = useStore($bindings)
  const railFocusHandoff = useStore($terminalRailFocusHandoff)
  const railRef = useRef<HTMLUListElement>(null)
  const focusSelectedTerminalRef = useRef(false)
  const [focusHandoffTerminalId, setFocusHandoffTerminalId] = useState<string | null>(null)
  const toggleHint = bindings['view.showTerminal']?.[0]
  const newHint = bindings['view.newTerminal']?.[0]

  // eslint-disable-next-line no-restricted-syntax -- one-shot focus request, not a reactive value mirror.
  useEffect(() => {
    if (!focusSelectedTerminalRef.current && !railFocusHandoff) {
      return
    }

    // TerminalWorkspace activates xterm from a passive effect and its first
    // resize frame. Two frames guarantee this runs after either sibling-effect
    // order, so the terminal cannot steal focus back from the selected rail tab.

    let focusFrame = 0

    const settleFrame = window.requestAnimationFrame(() => {
      focusFrame = window.requestAnimationFrame(() => {
        const selectedTab = railRef.current?.querySelector<HTMLElement>('[data-terminal-rail-tab][aria-selected="true"]')

        if (selectedTab) {
          railRef.current?.querySelector<HTMLElement>(`[${TERMINAL_RAIL_FOCUS_HANDOFF_ATTR}]`)?.removeAttribute(
            TERMINAL_RAIL_FOCUS_HANDOFF_ATTR
          )
          // Apply the marker synchronously before focus. A delayed xterm mount
          // can resolve at any later point and must yield while this tab owns
          // the close/roving handoff.
          selectedTab.setAttribute(TERMINAL_RAIL_FOCUS_HANDOFF_ATTR, '')
          setFocusHandoffTerminalId(selectedTab.dataset.terminalRailTab ?? null)
          selectedTab.focus()
        } else {
          setFocusHandoffTerminalId(null)
        }

        focusSelectedTerminalRef.current = false

        if (railFocusHandoff) {
          clearTerminalRailFocusHandoff()
        }
      })
    })

    return () => {
      window.cancelAnimationFrame(settleFrame)
      window.cancelAnimationFrame(focusFrame)
    }
  }, [activeId, railFocusHandoff, terminals])

  const requestSelectedTerminalFocus = () => {
    focusSelectedTerminalRef.current = true
  }

  const clearTerminalFocusHandoff = (id: string) => {
    setFocusHandoffTerminalId(current => (current === id ? null : current))
  }

  const closeTerminal = (id: string) => {
    requestSelectedTerminalFocus()

    if (!closeTerminalWithFocusRecovery(id)) {
      focusSelectedTerminalRef.current = false
    }
  }

  const closeOtherTerminals = (id: string) => {
    requestSelectedTerminalFocus()

    if (!closeOtherTerminalsWithFocusRecovery(id)) {
      focusSelectedTerminalRef.current = false
    }
  }

  const closeAllTerminals = () => {
    requestSelectedTerminalFocus()

    if (!closeAllTerminalsWithFocusRecovery()) {
      focusSelectedTerminalRef.current = false
    }
  }

  const handleTabKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    if (event.altKey || event.ctrlKey || event.metaKey) {
      return
    }

    const source = event.target instanceof Element ? event.target.closest<HTMLElement>('[data-terminal-rail-tab]') : null
    const currentId = source?.dataset.terminalRailTab
    const currentIndex = terminals.findIndex(term => term.id === currentId)

    if (currentIndex < 0) {
      return
    }

    const destination =
      event.key === 'ArrowDown'
        ? terminals[(currentIndex + 1) % terminals.length]
        : event.key === 'ArrowUp'
          ? terminals[(currentIndex - 1 + terminals.length) % terminals.length]
          : event.key === 'Home'
            ? terminals[0]
            : event.key === 'End'
              ? terminals.at(-1)
              : undefined

    if (!destination) {
      return
    }

    event.preventDefault()
    event.stopPropagation()
    requestSelectedTerminalFocus()
    selectTerminal(destination.id)
  }

  return (
    <div
      className="group/rail relative z-40 flex h-full w-9 shrink-0 flex-col items-center border-l border-(--ui-stroke-quaternary) bg-(--ui-terminal-surface-background)"
      // The rail sits at the pane's outer edge, under the collapsed sidebars'
      // hover-reveal triggers; mark it so those triggers go pointer-transparent
      // while it's hovered (see the suppression rules in styles.css) and a reach
      // for a tab can't drag in the file-browser/review panel.
      data-suppress-pane-reveal=""
      // The rail is part of the terminal focus scope: ⌘W closes its selected
      // terminal tab rather than dismissing the containing tree pane.
      data-terminal=""
    >
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto overflow-x-hidden overscroll-contain py-1 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        <ul
          aria-label={t.rightSidebar.terminalsAria}
          aria-orientation="vertical"
          className="m-0 flex list-none flex-col items-center gap-0.5 self-stretch p-0"
          onKeyDown={handleTabKeyDown}
          ref={railRef}
          role="tablist"
        >
          {terminals.map((term, index) => (
            <TerminalRailItem
              active={term.id === activeId}
              canCloseOthers={terminals.length > 1}
              closeAllTerminals={closeAllTerminals}
              closeOtherTerminals={closeOtherTerminals}
              closeTerminal={closeTerminal}
              focusHandoff={focusHandoffTerminalId === term.id}
              index={index}
              key={term.id}
              onFocusHandoffBlur={() => clearTerminalFocusHandoff(term.id)}
              term={term}
              toggleHint={toggleHint}
            />
          ))}
        </ul>
        <div className="flex w-full justify-center">
          <Tip
            label={<TipHintLabel hint={newHint && formatCombo(newHint)} text={t.rightSidebar.terminalNew} />}
            side="left"
          >
            <button
              aria-label={t.rightSidebar.terminalNew}
              className={cn(RAIL_ACTION, 'size-7 text-(--ui-text-quaternary)')}
              onClick={() => createTerminal()}
              type="button"
            >
              <Codicon name="add" size="0.8125rem" />
            </button>
          </Tip>
        </div>
      </div>

      <div className="flex shrink-0 flex-col items-center pb-1.5">
        <Tip label={t.rightSidebar.terminalHide} side="left">
          <button
            aria-label={t.rightSidebar.terminalHide}
            className={cn(RAIL_ACTION, 'opacity-0 transition-opacity group-hover/rail:opacity-100')}
            onClick={() => togglePaneVisible('terminal')}
            type="button"
          >
            <Codicon name="chevron-down" size="0.8125rem" />
          </button>
        </Tip>
      </div>
    </div>
  )
}

interface TerminalRailItemProps {
  active: boolean
  canCloseOthers: boolean
  closeAllTerminals: () => void
  closeOtherTerminals: (id: string) => void
  closeTerminal: (id: string) => void
  focusHandoff: boolean
  index: number
  onFocusHandoffBlur: () => void
  term: TerminalEntry
  toggleHint?: string
}

function TerminalRailItem({
  active,
  canCloseOthers,
  closeAllTerminals,
  closeOtherTerminals,
  closeTerminal,
  focusHandoff,
  index,
  onFocusHandoffBlur,
  term,
  toggleHint
}: TerminalRailItemProps) {
  const { t } = useI18n()
  const label = `${index + 1}. ${term.title}`

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <li className="relative flex w-full justify-center [-webkit-app-region:no-drag]" role="presentation">
          {active && (
            <span
              aria-hidden="true"
              className="absolute inset-y-0.5 right-0 w-0.5 rounded-l-sm bg-(--ui-stroke-primary)"
            />
          )}
          <Tip label={<TipHintLabel hint={toggleHint && formatCombo(toggleHint)} text={label} />} side="left">
            <button
              aria-controls={terminalPanelId(term.id)}
              aria-label={label}
              aria-selected={active}
              className={cn(
                'grid size-7 place-items-center rounded-md transition-colors',
                active
                  ? 'bg-(--chrome-action-hover) text-foreground'
                  : 'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
              )}
              {...middleClickHandlers(() => closeTerminal(term.id))}
              data-terminal-rail-focus-handoff={focusHandoff ? '' : undefined}
              data-terminal-rail-tab={term.id}
              id={terminalTabId(term.id)}
              // ⌘-click closes (the pane-tab gesture); a plain click selects.
              onBlur={onFocusHandoffBlur}
              onClick={event => (isMetaClose(event) ? closeTerminal(term.id) : selectTerminal(term.id))}
              role="tab"
              tabIndex={active ? 0 : -1}
              type="button"
            >
              <Codicon
                className={cn(term.kind === 'agent' && !active && 'text-primary')}
                name={term.kind === 'agent' ? 'agent' : 'terminal'}
                size="0.875rem"
              />
            </button>
          </Tip>
        </li>
      </ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem onSelect={() => closeTerminal(term.id)}>{t.common.close}</ContextMenuItem>
        <ContextMenuItem disabled={!canCloseOthers} onSelect={() => closeOtherTerminals(term.id)}>
          {t.rightSidebar.terminalCloseOthers}
        </ContextMenuItem>
        <ContextMenuItem onSelect={closeAllTerminals}>{t.rightSidebar.terminalCloseAll}</ContextMenuItem>
        <ContextMenuSeparator />
        <ContextMenuItem onSelect={() => togglePaneVisible('terminal')}>{t.rightSidebar.terminalHide}</ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  )
}
