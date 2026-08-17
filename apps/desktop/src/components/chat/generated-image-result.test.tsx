import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { GeneratedImage } from './generated-image-result'

const { generatedImageFromResult, gatewayMediaDataUrl, isRemoteGateway, mediaExternalUrl, openExternal, saveGatewayFile } =
  vi.hoisted(() => ({
    gatewayMediaDataUrl: vi.fn(),
    generatedImageFromResult: vi.fn(),
    isRemoteGateway: vi.fn(),
    mediaExternalUrl: vi.fn(),
    openExternal: vi.fn(),
    saveGatewayFile: vi.fn()
  }))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      assistant: { tool: { renderingImage: 'Rendering image…' } },
      desktop: { imageDownloadFailed: 'Image download failed', openImage: 'Open image' }
    }
  })
}))

vi.mock('@/lib/generated-images', () => ({ generatedImageFromResult }))

vi.mock('@/lib/media', () => ({
  filePathFromMediaPath: (path: string) => path,
  gatewayMediaDataUrl,
  isRemoteGateway,
  mediaExternalUrl,
  mediaName: (path: string) => path.split('/').pop() || path
}))

vi.mock('@/hooks/use-image-download', () => ({
  useImageDownload: () => ({ download: vi.fn(), saving: false })
}))

beforeEach(() => {
  vi.clearAllMocks()
  generatedImageFromResult.mockReturnValue('/outputs/render.png')
  // The inline load failing is what renders the "Open image" fallback link.
  gatewayMediaDataUrl.mockRejectedValue(new Error('gone'))
  ;(window as any).hermesDesktop = { openExternal, saveGatewayFile }
})

describe('GeneratedImage remote open fallback', () => {
  it('saves through the authenticated bridge instead of exposing a ?token= URL externally', async () => {
    isRemoteGateway.mockReturnValue(true)
    saveGatewayFile.mockResolvedValue({ path: '/home/me/Downloads/render.png', saved: true })

    render(<GeneratedImage result={{}} />)

    const link = await screen.findByText('Open image: render.png')

    fireEvent.click(link)

    await waitFor(() => {
      expect(saveGatewayFile).toHaveBeenCalledWith({ path: '/outputs/render.png', suggestedName: 'render.png' })
    })
    expect(openExternal).not.toHaveBeenCalled()
    // The token-carrying external URL must never even be constructed here.
    expect(mediaExternalUrl).not.toHaveBeenCalled()
  })

  it('surfaces a failure notice when the bridged save rejects', async () => {
    isRemoteGateway.mockReturnValue(true)
    saveGatewayFile.mockRejectedValue(new Error('gateway offline'))

    render(<GeneratedImage result={{}} />)

    fireEvent.click(await screen.findByText('Open image: render.png'))

    expect(await screen.findByText('Image download failed')).toBeTruthy()
  })

  it('keeps the external open for local connections', async () => {
    isRemoteGateway.mockReturnValue(false)
    mediaExternalUrl.mockReturnValue('file:///outputs/render.png')
    // Force the inline load to fail so the fallback link renders.
    ;(window as any).hermesDesktop.readFileDataUrl = vi.fn().mockRejectedValue(new Error('gone'))

    render(<GeneratedImage result={{}} />)

    fireEvent.click(await screen.findByText('Open image: render.png'))

    expect(openExternal).toHaveBeenCalledWith('file:///outputs/render.png')
    expect(saveGatewayFile).not.toHaveBeenCalled()
  })
})
