import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { isHttpUrl, PreviewToolbar } from './preview-toolbar'

const baseProps = {
  address: 'https://example.com',
  addressValid: true,
  canGoBack: false,
  canGoForward: false,
  loading: false,
  placeholder: 'https://example.com',
  onAddressBlur: vi.fn(),
  onAddressChange: vi.fn(),
  onAddressFocus: vi.fn(),
  onBack: vi.fn(),
  onForward: vi.fn(),
  onReload: vi.fn(),
  onSubmit: vi.fn(),
  onViewportChange: vi.fn(),
  viewport: { kind: 'free' as const }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('isHttpUrl', () => {
  it('accepts http and https URLs', () => {
    expect(isHttpUrl('https://example.com/path')).toBe(true)
    expect(isHttpUrl('http://localhost:3000/')).toBe(true)
  })

  it('rejects empty input', () => {
    expect(isHttpUrl('')).toBe(false)
    expect(isHttpUrl('   ')).toBe(false)
  })

  it('rejects non-web schemes', () => {
    expect(isHttpUrl('file:///etc/passwd')).toBe(false)
    expect(isHttpUrl('data:text/html,evil')).toBe(false)
    expect(isHttpUrl('javascript:alert(1)')).toBe(false)
    expect(isHttpUrl('about:blank')).toBe(false)
  })

  it('rejects non-URL strings', () => {
    expect(isHttpUrl('example')).toBe(false)
    expect(isHttpUrl('://broken')).toBe(false)
  })

  it('trims whitespace before validating', () => {
    expect(isHttpUrl('  https://example.com  ')).toBe(true)
  })
})

describe('PreviewToolbar', () => {
  it('renders all four controls with the right tooltips and labels', () => {
    const rendered = render(<PreviewToolbar {...baseProps} />)

    expect(rendered.getByRole('button', { name: 'Go back' })).toBeTruthy()
    expect(rendered.getByRole('button', { name: 'Go forward' })).toBeTruthy()
    expect(rendered.getByRole('button', { name: 'Reload preview' })).toBeTruthy()
    expect(rendered.getByRole('textbox', { name: 'Preview URL' })).toBeTruthy()
    expect(rendered.getAllByRole('button', { name: 'Navigate preview' }).length).toBe(1)
    expect(rendered.getByRole('form', { name: 'Navigate preview' })).toBeTruthy()
  })

  it('disables back and forward when there is no history', () => {
    const rendered = render(<PreviewToolbar {...baseProps} canGoBack={false} canGoForward={false} />)

    expect((rendered.getByRole('button', { name: 'Go back' }) as HTMLButtonElement).disabled).toBe(true)
    expect((rendered.getByRole('button', { name: 'Go forward' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('enables back and forward when there is history', () => {
    const rendered = render(<PreviewToolbar {...baseProps} canGoBack={true} canGoForward={true} />)

    expect((rendered.getByRole('button', { name: 'Go back' }) as HTMLButtonElement).disabled).toBe(false)
    expect((rendered.getByRole('button', { name: 'Go forward' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('marks the address invalid when addressValid is false and the input has content', () => {
    const rendered = render(<PreviewToolbar {...baseProps} address="not a url" addressValid={false} />)

    const input = rendered.getByRole('textbox', { name: 'Preview URL' }) as HTMLInputElement

    expect(input.getAttribute('aria-invalid')).toBe('true')
    expect((rendered.getByRole('button', { name: 'Navigate preview' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('does not mark the address invalid when the input is empty (no aria-invalid)', () => {
    const rendered = render(<PreviewToolbar {...baseProps} address="" addressValid={false} />)

    const input = rendered.getByRole('textbox', { name: 'Preview URL' }) as HTMLInputElement

    expect(input.getAttribute('aria-invalid')).toBeNull()
    expect((rendered.getByRole('button', { name: 'Navigate preview' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('fires onBack when the back button is clicked', () => {
    const onBack = vi.fn()
    const rendered = render(<PreviewToolbar {...baseProps} canGoBack={true} onBack={onBack} />)

    fireEvent.click(rendered.getByRole('button', { name: 'Go back' }))

    expect(onBack).toHaveBeenCalledOnce()
  })

  it('fires onForward when the forward button is clicked', () => {
    const onForward = vi.fn()
    const rendered = render(<PreviewToolbar {...baseProps} canGoForward={true} onForward={onForward} />)

    fireEvent.click(rendered.getByRole('button', { name: 'Go forward' }))

    expect(onForward).toHaveBeenCalledOnce()
  })

  it('fires onReload when the reload button is clicked', () => {
    const onReload = vi.fn()
    const rendered = render(<PreviewToolbar {...baseProps} onReload={onReload} />)

    fireEvent.click(rendered.getByRole('button', { name: 'Reload preview' }))

    expect(onReload).toHaveBeenCalledOnce()
  })

  it('submits the trimmed address on form submit when addressValid is true', () => {
    const onSubmit = vi.fn()

    const rendered = render(
      <PreviewToolbar {...baseProps} address="  https://example.com/path  " onSubmit={onSubmit} />
    )

    fireEvent.submit(rendered.getByRole('form', { name: 'Navigate preview' }))

    expect(onSubmit).toHaveBeenCalledWith('https://example.com/path')
  })

  it('does not submit when the address is invalid', () => {
    const onSubmit = vi.fn()

    const rendered = render(
      <PreviewToolbar {...baseProps} address="not a url" addressValid={false} onSubmit={onSubmit} />
    )

    fireEvent.submit(rendered.getByRole('form', { name: 'Navigate preview' }))

    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('fires onAddressChange on every keystroke without clobbering', () => {
    const onAddressChange = vi.fn()

    const rendered = render(<PreviewToolbar {...baseProps} address="https://exam" onAddressChange={onAddressChange} />)

    const input = rendered.getByRole('textbox', { name: 'Preview URL' }) as HTMLInputElement

    fireEvent.change(input, { target: { value: 'https://example' } })

    expect(onAddressChange).toHaveBeenCalledWith('https://example')
  })

  it('fires onAddressBlur when the input loses focus', () => {
    const onAddressBlur = vi.fn()
    const rendered = render(<PreviewToolbar {...baseProps} onAddressBlur={onAddressBlur} />)

    const input = rendered.getByRole('textbox', { name: 'Preview URL' }) as HTMLInputElement

    fireEvent.blur(input)

    expect(onAddressBlur).toHaveBeenCalledOnce()
  })

  it('fires onAddressFocus when the input is focused', () => {
    const onAddressFocus = vi.fn()
    const rendered = render(<PreviewToolbar {...baseProps} onAddressFocus={onAddressFocus} />)

    const input = rendered.getByRole('textbox', { name: 'Preview URL' }) as HTMLInputElement

    fireEvent.focus(input)

    expect(onAddressFocus).toHaveBeenCalledOnce()
  })

  it('rotates the reload icon while loading', () => {
    const { container } = render(<PreviewToolbar {...baseProps} loading={true} />)

    const reloadButton = container.querySelector('button[aria-label="Reload preview"]')
    const reloadIcon = reloadButton?.querySelector('svg')

    expect(reloadIcon).toBeTruthy()
    expect(reloadIcon?.getAttribute('class') ?? '').toContain('animate-spin')
  })

  it('does not rotate the reload icon when idle', () => {
    const { container } = render(<PreviewToolbar {...baseProps} loading={false} />)

    const reloadButton = container.querySelector('button[aria-label="Reload preview"]')
    const reloadIcon = reloadButton?.querySelector('svg')

    expect(reloadIcon).toBeTruthy()
    expect(reloadIcon?.getAttribute('class') ?? '').not.toContain('animate-spin')
  })
})
