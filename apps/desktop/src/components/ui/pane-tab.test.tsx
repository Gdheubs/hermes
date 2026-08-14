import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider, setRuntimeI18nLocale, useI18n } from '@/i18n'

import { PaneTab, PaneTabLabel } from './pane-tab'

function LocaleSwitchingPaneTab() {
  const { setLocale } = useI18n()

  return (
    <>
      <button onClick={() => void setLocale('zh')} type="button">
        Switch to Chinese
      </button>
      <PaneTab onClose={vi.fn()}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    </>
  )
}

afterEach(() => {
  cleanup()
  setRuntimeI18nLocale('en')
})

describe('PaneTab close gestures', () => {
  it('middle-click closes — pointer events only, no auxclick', () => {
    const onClose = vi.fn()
    render(
      <PaneTab onClose={onClose}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    const tab = screen.getByText('tab')
    fireEvent.pointerDown(tab, { button: 1 })
    fireEvent.pointerUp(tab, { button: 1 })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('⌘-click (metaKey + button 0) closes — the Mac middle-click equivalent', () => {
    const onClose = vi.fn()
    render(
      <PaneTab onClose={onClose}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    fireEvent.pointerDown(screen.getByText('tab'), { button: 0, metaKey: true })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('⌘-click preempts the shell drag/activate pointerdown handler', () => {
    const onClose = vi.fn()
    const onPointerDown = vi.fn()
    render(
      <PaneTab onClose={onClose} onPointerDown={onPointerDown}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    fireEvent.pointerDown(screen.getByText('tab'), { button: 0, metaKey: true })
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onPointerDown).not.toHaveBeenCalled()
  })

  it('⌘-click swallows the follow-up activation click (capture phase)', () => {
    const onClose = vi.fn()
    const onActivate = vi.fn()
    render(
      <PaneTab onClose={onClose}>
        <PaneTabLabel as="button" onClick={onActivate}>
          tab
        </PaneTabLabel>
      </PaneTab>
    )

    fireEvent.click(screen.getByText('tab'), { button: 0, metaKey: true })
    expect(onActivate).not.toHaveBeenCalled()
  })

  it('plain left-click neither closes nor blocks activation', () => {
    const onClose = vi.fn()
    const onActivate = vi.fn()
    const onPointerDown = vi.fn()
    render(
      <PaneTab onClose={onClose} onPointerDown={onPointerDown}>
        <PaneTabLabel as="button" onClick={onActivate}>
          tab
        </PaneTabLabel>
      </PaneTab>
    )

    fireEvent.pointerDown(screen.getByText('tab'), { button: 0 })
    fireEvent.click(screen.getByText('tab'), { button: 0 })
    expect(onClose).not.toHaveBeenCalled()
    expect(onPointerDown).toHaveBeenCalledTimes(1)
    expect(onActivate).toHaveBeenCalledTimes(1)
  })

  it('does nothing without an onClose (uncloseable workspace tab)', () => {
    const onPointerDown = vi.fn()
    render(
      <PaneTab onPointerDown={onPointerDown}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    fireEvent.pointerDown(screen.getByText('tab'), { button: 0, metaKey: true })
    expect(onPointerDown).toHaveBeenCalledTimes(1)
  })
})

describe('PaneTab close button', () => {
  it('keeps the tab and its close button as sibling controls in the tablist', () => {
    const visualWrapperRef = createRef<HTMLDivElement>()
    const onKeyDown = vi.fn()

    render(
      <div role="tablist">
        <PaneTab
          aria-controls="panel-a"
          aria-label="Messages"
          aria-selected={false}
          data-tree-tab="pane-a"
          id="tab-a"
          onClose={vi.fn()}
          onKeyDown={onKeyDown}
          ref={visualWrapperRef}
          role="tab"
          style={{ cursor: 'grab' }}
        >
          <PaneTabLabel>tab</PaneTabLabel>
        </PaneTab>
      </div>
    )

    const tab = screen.getByRole('tab', { name: 'Messages' })
    const closeButton = screen.getByRole('button', { name: 'Close tab' })

    expect(tab.contains(closeButton)).toBe(false)
    expect(tab.parentElement).toBe(closeButton.parentElement)
    expect(tab.parentElement?.getAttribute('role')).toBe('presentation')
    expect(tab.id).toBe('tab-a')
    expect(tab.getAttribute('aria-controls')).toBe('panel-a')
    expect(tab.getAttribute('aria-selected')).toBe('false')
    expect(visualWrapperRef.current).toBe(tab.parentElement)
    expect(visualWrapperRef.current?.dataset.treeTab).toBe('pane-a')
    expect(visualWrapperRef.current?.style.cursor).toBe('grab')

    fireEvent.keyDown(tab, { key: 'Enter' })
    expect(onKeyDown).toHaveBeenCalledTimes(1)
  })

  it('updates the close button label when the provider locale changes after mount', async () => {
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <LocaleSwitchingPaneTab />
      </I18nProvider>
    )

    expect(screen.getByRole('button', { name: 'Close tab' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Switch to Chinese' }))

    expect(await screen.findByRole('button', { name: '关闭标签' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Close tab' })).toBeNull()
  })

  it('reveals the close button when keyboard focus reaches it', () => {
    render(
      <PaneTab onClose={vi.fn()}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    const closeButton = screen.getByRole('button', { name: 'Close tab' })
    const closeIcon = closeButton.querySelector<HTMLElement>('[data-slot="pane-tab-close-icon"]')!

    expect(closeIcon.style.opacity).toBe('0')

    act(() => closeButton.focus())

    expect(closeButton.ownerDocument.activeElement).toBe(closeButton)
    expect(closeIcon.style.opacity).toBe('1')
  })

  it('swaps the dirty dot for the close glyph on hover and keyboard focus', () => {
    const { container } = render(
      <PaneTab dirty onClose={vi.fn()}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    const dirtyIndicator = container.querySelector<HTMLElement>('[data-slot="pane-tab-dirty-indicator"]')!
    const closeButton = screen.getByRole('button', { name: 'Close tab' })
    const closeIcon = closeButton.querySelector<HTMLElement>('[data-slot="pane-tab-close-icon"]')!
    const visualWrapper = closeButton.parentElement!

    expect(dirtyIndicator.style.opacity).toBe('1')
    expect(closeIcon.style.opacity).toBe('0')

    fireEvent.mouseEnter(visualWrapper)
    expect(dirtyIndicator.style.opacity).toBe('0')
    expect(closeIcon.style.opacity).toBe('1')

    fireEvent.mouseLeave(visualWrapper)
    expect(dirtyIndicator.style.opacity).toBe('1')
    expect(closeIcon.style.opacity).toBe('0')

    act(() => closeButton.focus())
    expect(closeButton.ownerDocument.activeElement).toBe(closeButton)
    expect(dirtyIndicator.style.opacity).toBe('0')
    expect(closeIcon.style.opacity).toBe('1')
  })

  it('clicking the close button calls onClose and stops propagation', () => {
    const onClose = vi.fn()
    const onStripClick = vi.fn()
    const onStripPointerDown = vi.fn()
    const onTabPointerDown = vi.fn()
    render(
      <div onClick={onStripClick} onPointerDown={onStripPointerDown}>
        <PaneTab onClose={onClose} onPointerDown={onTabPointerDown}>
          <PaneTabLabel>tab</PaneTabLabel>
        </PaneTab>
      </div>
    )

    const closeBtn = screen.getByRole('button', { name: 'Close tab' })
    fireEvent.pointerDown(closeBtn)
    fireEvent.click(closeBtn)
    expect(onClose).toHaveBeenCalledTimes(1)
    // The tab's own pointerdown handler must NOT fire — the X is a leaf action.
    expect(onTabPointerDown).not.toHaveBeenCalled()
    expect(onStripPointerDown).not.toHaveBeenCalled()
    expect(onStripClick).not.toHaveBeenCalled()
  })

  it('middle-clicking the close-control area closes once without reaching the tab strip', () => {
    const onClose = vi.fn()
    const onStripPointerDown = vi.fn()
    render(
      <div onPointerDown={onStripPointerDown}>
        <PaneTab onClose={onClose}>
          <PaneTabLabel>tab</PaneTabLabel>
        </PaneTab>
      </div>
    )

    const closeButton = screen.getByRole('button', { name: 'Close tab' })
    fireEvent.pointerDown(closeButton, { button: 1 })
    fireEvent.mouseDown(closeButton, { button: 1 })
    fireEvent.pointerUp(closeButton, { button: 1 })

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onStripPointerDown).not.toHaveBeenCalled()
  })

  it('⌘-clicking the close-control area closes once without reaching the tab strip', () => {
    const onClose = vi.fn()
    const onStripClick = vi.fn()
    const onStripPointerDown = vi.fn()
    render(
      <div onClick={onStripClick} onPointerDown={onStripPointerDown}>
        <PaneTab onClose={onClose}>
          <PaneTabLabel>tab</PaneTabLabel>
        </PaneTab>
      </div>
    )

    const closeButton = screen.getByRole('button', { name: 'Close tab' })
    fireEvent.pointerDown(closeButton, { button: 0, metaKey: true })
    fireEvent.mouseDown(closeButton, { button: 0, metaKey: true })
    fireEvent.click(closeButton, { button: 0, metaKey: true })

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onStripPointerDown).not.toHaveBeenCalled()
    expect(onStripClick).not.toHaveBeenCalled()
  })

  it('leaves right-click and context-menu events over the close control alone', () => {
    const onClose = vi.fn()
    const onStripPointerDown = vi.fn()
    const onContextMenu = vi.fn()
    render(
      <div onContextMenu={onContextMenu} onPointerDown={onStripPointerDown}>
        <PaneTab onClose={onClose}>
          <PaneTabLabel>tab</PaneTabLabel>
        </PaneTab>
      </div>
    )

    const closeButton = screen.getByRole('button', { name: 'Close tab' })
    fireEvent.pointerDown(closeButton, { button: 2 })
    fireEvent.contextMenu(closeButton)

    expect(onClose).not.toHaveBeenCalled()
    expect(onStripPointerDown).toHaveBeenCalledTimes(1)
    expect(onContextMenu).toHaveBeenCalledTimes(1)
  })

  it('does not render a close button on vertical tabs', () => {
    const onClose = vi.fn()
    render(
      <PaneTab onClose={onClose} vertical>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    expect(screen.queryByRole('button', { name: 'Close tab' })).toBeNull()
  })

  it('does not render a close button without onClose', () => {
    render(
      <PaneTab>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    expect(screen.queryByRole('button', { name: 'Close tab' })).toBeNull()
  })
})
