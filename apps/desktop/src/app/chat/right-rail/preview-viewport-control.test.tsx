import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PreviewViewportControl } from './preview-viewport-control'

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', TestResizeObserver)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('PreviewViewportControl', () => {
  it('opens the picker and selects a preset', () => {
    const onModeChange = vi.fn()
    const rendered = render(<PreviewViewportControl mode={{ kind: 'free' }} onModeChange={onModeChange} />)

    fireEvent.click(rendered.getByRole('button', { name: 'Preview viewport' }))
    fireEvent.click(rendered.getByRole('option', { name: /Mobile/ }))

    expect(onModeChange).toHaveBeenCalledWith({ kind: 'preset', id: 'mobile' })
  })

  it('applies a custom size', () => {
    const onModeChange = vi.fn()
    const rendered = render(<PreviewViewportControl mode={{ kind: 'free' }} onModeChange={onModeChange} />)

    fireEvent.click(rendered.getByRole('button', { name: 'Preview viewport' }))
    fireEvent.change(rendered.getByRole('textbox', { name: 'Viewport width' }), { target: { value: '390' } })
    fireEvent.change(rendered.getByRole('textbox', { name: 'Viewport height' }), { target: { value: '844' } })
    fireEvent.submit(rendered.getByRole('textbox', { name: 'Viewport width' }).closest('form')!)

    expect(onModeChange).toHaveBeenCalledWith({ kind: 'custom', width: 390, height: 844 })
  })
})
