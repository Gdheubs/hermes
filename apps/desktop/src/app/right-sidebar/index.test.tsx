import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesReadDirResult } from '@/global'
import {
  $connection,
  commitWorkspaceCwdForSelectedSession,
  releaseWorkspaceCwdOwner,
  setCurrentCwd,
  setSelectedStoredSessionId
} from '@/store/session'

import { resetProjectTreeState } from './files/use-project-tree'

import { RightSidebarPane } from './index'

const readDir = vi.fn<(path: string) => Promise<HermesReadDirResult>>()

function installBridge() {
  ;(window as unknown as { hermesDesktop: { readDir: typeof readDir } }).hermesDesktop = { readDir }
}

describe('RightSidebarPane', () => {
  beforeEach(() => {
    $connection.set(null)
    setSelectedStoredSessionId(null)
    commitWorkspaceCwdForSelectedSession('')
    resetProjectTreeState()
    readDir.mockReset()
    readDir.mockResolvedValue({ entries: [{ isDirectory: false, name: 'README.md', path: '/repo/README.md' }] })
    installBridge()
  })

  afterEach(() => {
    cleanup()
    $connection.set(null)
    setCurrentCwd('')
    resetProjectTreeState()
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('renders the tree whenever the session has a working dir (repo or not) — no picker', async () => {
    setCurrentCwd('/repo')

    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)

    const refresh = await screen.findByRole('button', { name: 'Refresh tree' })

    readDir.mockClear()
    fireEvent.click(refresh)
    await waitFor(() => expect(readDir).toHaveBeenCalledWith('/repo'))

    // The freeform folder picker is retired.
    expect(screen.queryByRole('button', { name: 'Open folder' })).toBeNull()
  })

  it('shows no tree for a detached chat (no working dir)', async () => {
    setCurrentCwd('')

    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)

    await waitFor(() => expect(screen.queryByRole('button', { name: 'Refresh tree' })).toBeNull())
    expect(readDir).not.toHaveBeenCalled()
  })

  it('hides the previous repo while the selected session workspace is unresolved', async () => {
    setSelectedStoredSessionId('session-a')
    commitWorkspaceCwdForSelectedSession('/repo-a')
    readDir.mockResolvedValueOnce({
      entries: [{ isDirectory: false, name: 'a.txt', path: '/repo-a/a.txt' }]
    })

    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)
    await waitFor(() => expect(readDir).toHaveBeenCalledWith('/repo-a'))
    expect(screen.getByText('repo-a')).toBeTruthy()

    act(() => {
      setSelectedStoredSessionId('session-b')
      releaseWorkspaceCwdOwner()
    })

    await waitFor(() => expect(screen.queryByRole('button', { name: 'Refresh tree' })).toBeNull())
    expect(screen.queryByText('repo-a')).toBeNull()
    expect(readDir).toHaveBeenCalledTimes(1)
  })

  it('follows rapid session switches and ignores an old repo load that settles last', async () => {
    let resolveFirstA: ((value: HermesReadDirResult) => void) | undefined
    readDir.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          resolveFirstA = resolve
        })
    )
    readDir.mockResolvedValueOnce({
      entries: [{ isDirectory: false, name: 'b.txt', path: '/repo-b/b.txt' }]
    })
    readDir.mockResolvedValueOnce({
      entries: [{ isDirectory: false, name: 'latest-a.txt', path: '/repo-a/latest-a.txt' }]
    })

    setSelectedStoredSessionId('session-a')
    commitWorkspaceCwdForSelectedSession('/repo-a')
    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)
    await waitFor(() => expect(readDir).toHaveBeenCalledWith('/repo-a'))

    act(() => {
      setSelectedStoredSessionId('session-b')
      releaseWorkspaceCwdOwner()
      commitWorkspaceCwdForSelectedSession('/repo-b')
    })
    await waitFor(() => expect(readDir).toHaveBeenCalledWith('/repo-b'))
    expect(screen.getByText('repo-b')).toBeTruthy()

    act(() => {
      setSelectedStoredSessionId('session-a')
      releaseWorkspaceCwdOwner()
      commitWorkspaceCwdForSelectedSession('/repo-a')
    })
    await waitFor(() => expect(readDir.mock.calls.filter(([path]) => path === '/repo-a')).toHaveLength(2))
    expect(screen.getByText('repo-a')).toBeTruthy()

    await act(async () => {
      resolveFirstA?.({ entries: [{ isDirectory: false, name: 'stale-a.txt', path: '/repo-a/stale-a.txt' }] })
      await Promise.resolve()
    })

    expect(screen.getByText('repo-a')).toBeTruthy()
    expect(screen.queryByText('repo-b')).toBeNull()
  })
})
