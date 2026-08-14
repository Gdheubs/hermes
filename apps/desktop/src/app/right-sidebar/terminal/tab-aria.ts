const terminalDomId = (id: string) => encodeURIComponent(id)

export const terminalPanelId = (id: string) => `terminal-panel-${terminalDomId(id)}`

export const terminalTabId = (id: string) => `terminal-tab-${terminalDomId(id)}`
