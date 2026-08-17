// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { group, split } from '@/components/pane-shell/tree/model'
import { $layoutTree, noteActiveTreeGroup } from '@/components/pane-shell/tree/store'
import { SidebarProvider } from '@/components/ui/sidebar'
import { registry } from '@/contrib/registry'
import { $selectedStoredSessionId } from '@/store/session'

import { ROUTES_AREA, SIDEBAR_NAV_AREA } from '../../routes'

import { ChatSidebar } from './index'

const noop = () => {}

const noopAsync = async () => {}

const renderSidebar = () =>
  render(
    <MemoryRouter initialEntries={['/kanban']}>
      <SidebarProvider>
        <ChatSidebar
          currentView="extension"
          onArchiveSession={noop}
          onBranchSession={noop}
          onDeleteSession={noop}
          onLoadMoreSessions={noop}
          onManageCronJob={noop}
          onNavigate={noop}
          onNewSessionInWorkspace={noop}
          onNewSessionSplit={noop}
          onResumeSession={noop}
          onTriggerCronJob={noopAsync}
        />
      </SidebarProvider>
    </MemoryRouter>
  )

const currentButtons = () =>
  screen.queryAllByRole('button').filter(button => button.getAttribute('aria-current') === 'page')

const activeButtons = () => screen.queryAllByRole('button').filter(button => button.dataset.active === 'true')

const expectOnlyCurrent = (button: HTMLElement | null) => {
  expect(currentButtons()).toEqual(button ? [button] : [])
  expect(activeButtons()).toEqual(button ? [button] : [])
}

describe('ChatSidebar contributed navigation', () => {
  let dispose: () => void

  beforeEach(() => {
    dispose = registry.registerMany([
      { area: ROUTES_AREA, id: 'kanban-page', data: { path: '/kanban' }, render: () => null },
      { area: ROUTES_AREA, id: 'reports-page', data: { path: '/reports' }, render: () => null },
      { area: SIDEBAR_NAV_AREA, id: 'kanban-nav', data: { codicon: 'project', label: 'Kanban', path: '/kanban' } },
      { area: SIDEBAR_NAV_AREA, id: 'reports-nav', data: { codicon: 'graph', label: 'Reports', path: '/reports' } }
    ])
    $selectedStoredSessionId.set('tile-one')
    $layoutTree.set(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'workspace-group' }),
        group(['session-tile:tile-one'], { active: 'session-tile:tile-one', id: 'tile-one-group' }),
        group(['session-tile:tile-two'], { active: 'session-tile:tile-two', id: 'tile-two-group' })
      ])
    )
    noteActiveTreeGroup('workspace-group')
  })

  afterEach(() => {
    cleanup()
    dispose()
    $selectedStoredSessionId.set(null)
    $layoutTree.set(null)
  })

  it('tracks the visible surface across routes tiles and null identities', () => {
    renderSidebar()

    const kanban = screen.getByRole('button', { name: 'Kanban' })
    const reports = screen.getByRole('button', { name: 'Reports' })

    expectOnlyCurrent(kanban)
    expect(reports.getAttribute('aria-current')).toBeNull()
    expect(reports.getAttribute('data-active')).not.toBe('true')

    act(() => noteActiveTreeGroup('tile-one-group'))
    expectOnlyCurrent(null)
    expect(kanban.getAttribute('aria-current')).toBeNull()
    expect(kanban.getAttribute('data-active')).not.toBe('true')

    act(() => noteActiveTreeGroup('tile-two-group'))
    expectOnlyCurrent(null)

    act(() => noteActiveTreeGroup('workspace-group'))
    expectOnlyCurrent(kanban)

    act(() => $selectedStoredSessionId.set(null))
    expectOnlyCurrent(kanban)

    act(() => noteActiveTreeGroup('tile-one-group'))
    expectOnlyCurrent(null)

    act(() => {
      $layoutTree.set(group(['workspace'], { active: 'workspace', id: 'workspace-group' }))
      noteActiveTreeGroup('workspace-group')
    })
    expectOnlyCurrent(kanban)
  })
})
