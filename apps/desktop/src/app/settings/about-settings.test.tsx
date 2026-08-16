import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopUpdateStatus, DesktopVersionInfo } from '@/global'

const VERSION: DesktopVersionInfo = {
  appVersion: '0.17.0',
  electronVersion: '39.2.7',
  nodeVersion: '22.20.0',
  platform: 'darwin',
  hermesRoot: '/Users/private-name/.hermes/hermes-agent'
}

const STATUS: DesktopUpdateStatus = {
  supported: true,
  currentSha: '0123456789abcdef0123456789abcdef01234567'
}

const desktopVersion = atom<DesktopVersionInfo | null>(VERSION)
const updateStatus = atom<DesktopUpdateStatus | null>(STATUS)

vi.mock('@/store/updates', () => ({
  $desktopVersion: desktopVersion,
  $updateApply: atom({ applying: false, stage: 'idle' }),
  $updateChecking: atom(false),
  $updateStatus: updateStatus,
  checkUpdates: vi.fn(),
  openUpdatesWindow: vi.fn(),
  refreshDesktopVersion: vi.fn().mockResolvedValue(VERSION),
  startActiveUpdate: vi.fn()
}))

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

beforeEach(() => {
  desktopVersion.set(VERSION)
  updateStatus.set(STATUS)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()

  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('AboutSettings support', () => {
  it('opens a reviewed GitHub issue draft through the Desktop bridge', async () => {
    const openExternal = vi.fn().mockResolvedValue(undefined)
    desktopWindow.hermesDesktop = { openExternal } as unknown as Window['hermesDesktop']
    const { AboutSettings } = await import('./about-settings')

    render(<AboutSettings />)
    fireEvent.click(screen.getByRole('button', { name: 'Report on GitHub' }))

    expect(openExternal).toHaveBeenCalledOnce()
    const report = new URL(openExternal.mock.calls[0][0])

    expect(`${report.origin}${report.pathname}`).toBe('https://github.com/NousResearch/hermes-agent/issues/new')
    expect(report.searchParams.get('title')).toBe('[Desktop Bug]: ')
    expect(report.searchParams.get('body')).toContain('- Desktop version: 0.17.0')
  })
})
