import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { $backgroundStatusBySession } from '@/store/composer-status'

import { seedAgentTerminalCommand, syncAgentTerminalSnapshot } from './agent-terminal-stream'
import { setActiveTerminalId } from './buffer'
import { AgentTerminalInstance, TerminalInstance } from './instance'
import { terminalPanelId, terminalTabId } from './tab-aria'
import { $activeTerminalId, $terminals, ensureAgentTerminal, selectTerminal } from './terminals'

interface TerminalWorkspaceProps {
  onAddSelectionToChat: (text: string, label?: string) => void
}

/** The persistent-overlay layer: the stack of live xterm instances (only these
 *  must stay in the fixed overlay, for the WebGL host). Mount/visibility is owned
 *  by PersistentTerminal (latched so shells survive hiding); the tab rail and
 *  new-terminal control live in the pane DOM — see TerminalPaneChrome. */
export function TerminalWorkspace({ onAddSelectionToChat }: TerminalWorkspaceProps) {
  const terminals = useStore($terminals)
  const activeId = useStore($activeTerminalId)
  const background = useStore($backgroundStatusBySession)

  // Mirror the tab selection into the agent reader (read_terminal reads it).
  useEffect(() => {
    const unsubscribe = $activeTerminalId.subscribe(setActiveTerminalId)

    return () => {
      unsubscribe()
      setActiveTerminalId(null)
    }
  }, [])

  // Surface the agent's background processes as read-only tabs (once each).
  // Live chunks stream via agent.terminal.output; the process-list snapshot also
  // seeds/falls back so the tab never stays blank if the stream races startup.
  useEffect(() => {
    for (const list of Object.values(background)) {
      for (const item of list) {
        ensureAgentTerminal(item.id, item.title)
        seedAgentTerminalCommand(item.id, item.title)
        syncAgentTerminalSnapshot(item.id, item.output ?? '')
      }
    }
  }, [background])

  return (
    <>
      {terminals.map(term => {
        const active = term.id === activeId

        return (
          <div
            aria-hidden={!active}
            aria-labelledby={terminalTabId(term.id)}
            data-terminal=""
            data-terminal-id={term.id}
            id={terminalPanelId(term.id)}
            key={term.id}
            onFocusCapture={() => selectTerminal(term.id)}
            role="tabpanel"
          >
            {term.kind === 'agent' ? (
              <AgentTerminalInstance active={active} id={term.id} procId={term.procId!} />
            ) : (
              <TerminalInstance
                active={active}
                cwd={term.cwd}
                id={term.id}
                onAddSelectionToChat={onAddSelectionToChat}
                restoreCwd={term.restoreCwd}
                reviveBuffer={term.reviveBuffer}
              />
            )}
          </div>
        )
      })}
    </>
  )
}
