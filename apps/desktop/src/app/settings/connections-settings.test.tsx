import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopConnectionsRegistry } from '@/global'
import { $connection } from '@/store/session'

import { ConnectionsSettings } from './connections-settings'

const list = vi.fn()
const save = vi.fn()
const remove = vi.fn()
const setLaunchMode = vi.fn()
const setPrimary = vi.fn()
const test = vi.fn()

const registry: DesktopConnectionsRegistry = {
  connections: [
    { id: 'local', kind: 'local', label: 'This device', tokenPreview: null, tokenSet: false },
    {
      authMode: 'token',
      id: 'homelab',
      kind: 'remote',
      label: 'Homelab',
      tokenPreview: '...abc123',
      tokenSet: true,
      url: 'http://homelab.lan:9119'
    }
  ],
  primary: 'local',
  secureTokenStorage: true,
  version: 2
}

beforeEach(() => {
  $connection.set({
    baseUrl: 'http://homelab.lan:9119',
    connectionId: 'homelab',
    isFullscreen: false,
    logs: [],
    mode: 'remote',
    nativeOverlayWidth: 0,
    token: 'test-token',
    windowButtonPosition: null,
    wsUrl: 'ws://homelab.lan:9119/ws'
  })
  list.mockResolvedValue(registry)
  save.mockResolvedValue({ connection: registry.connections[1], ok: true, registry })
  remove.mockResolvedValue({ ok: true, registry: { ...registry, connections: [registry.connections[0]] } })
  setPrimary.mockResolvedValue({ ok: true, registry: { ...registry, primary: 'homelab' } })
  setLaunchMode.mockResolvedValue({ ok: true, registry: { ...registry, launchMode: 'last-used' } })
  test.mockResolvedValue({ ok: true, reachable: true })
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { connections: { list, remove, save, setLaunchMode, setPrimary, test } }
  })
})

afterEach(() => {
  $connection.set(null)
  cleanup()
  vi.clearAllMocks()
})

describe('ConnectionsSettings', () => {
  it('distinguishes the current connection from the registry primary', async () => {
    render(<ConnectionsSettings />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    // Label and the managed pill share the copy, so expect both instances.
    expect(screen.getAllByText('This device').length).toBeGreaterThan(0)
    expect(screen.getByText('Current')).toBeTruthy()
    expect(screen.getByText('Primary')).toBeTruthy()
    expect(list).toHaveBeenCalledTimes(1)
  })

  it('opens the add-connection editor and saves with a required label', async () => {
    render(<ConnectionsSettings />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))

    // Save is disabled until a label is present.
    const saveButton = screen.getByText('Save connection').closest('button')!
    expect(saveButton.disabled).toBe(true)

    fireEvent.change(screen.getByPlaceholderText('Homelab'), { target: { value: 'Spark box' } })
    fireEvent.change(screen.getByPlaceholderText('http://homelab.lan:9119'), {
      target: { value: 'http://spark.lan:9119' }
    })
    expect(saveButton.disabled).toBe(false)
    fireEvent.click(saveButton)

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({
      kind: 'remote',
      label: 'Spark box',
      url: 'http://spark.lan:9119'
    })
  })

  it('makes a non-primary connection primary', async () => {
    render(<ConnectionsSettings />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Make primary'))

    await waitFor(() => expect(setPrimary).toHaveBeenCalledWith('homelab'))
  })

  it('lets users opt into restoring the last-used source', async () => {
    render(<ConnectionsSettings />)

    await waitFor(() => expect(screen.getByText('Open on launch')).toBeTruthy())
    fireEvent.click(screen.getByText('Last used'))

    await waitFor(() => expect(setLaunchMode).toHaveBeenCalledWith('last-used'))
  })

  it('keeps the launch preference out of the way for a single source', async () => {
    list.mockResolvedValueOnce({ ...registry, connections: [registry.connections[0]] })

    render(<ConnectionsSettings />)

    await waitFor(() => expect(list).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('Open on launch')).toBeNull()
  })

  it('tests a connection through the bridge', async () => {
    render(<ConnectionsSettings />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getAllByText('Test')[0])

    await waitFor(() => expect(test).toHaveBeenCalled())
  })
})
