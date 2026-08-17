import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { HERMES_QUOTE_MIME } from '@/app/chat/hooks/use-composer-actions'
import { useDragTextToComposer } from '@/components/assistant-ui/thread/use-drag-text-to-composer'

// The drag-text-to-composer hook is a thin wrapper around the HTML5 Drag and
// Drop API. Tests exercise the handler's behavior through a simulated
// DragEvent, verifying that:
// - Selected text is formatted as quoted lines and set on the DataTransfer
// - effectAllowed is 'copy'
// - No selection → event is prevented (no-op)
// - dragend (component- or document-level) cleans up the ghost
// - A stale ghost is released before a new drag starts

// Minimal DataTransfer stand-in for jsdom (no native DataTransfer constructor).
interface StubDataTransfer {
  effectAllowed: string
  getData: (format: string) => string
  setData: (format: string, data: string) => void
  types: string[]
}

function stubDataTransfer(): StubDataTransfer {
  const store: Record<string, string> = {}

  return {
    effectAllowed: 'none',
    getData: (format: string) => store[format] ?? '',
    setData: (format: string, data: string) => {
      store[format] = data
    },
    types: []
  }
}

/** The drag ghost is the only fixed-position element createDragGhost appends. */
function ghostCount(): number {
  return Array.from(document.body.children).filter(el => (el as HTMLElement).style.position === 'fixed').length
}

describe('useDragTextToComposer', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    // Release any module-level ghost a test left behind. Dispatching dragend
    // exercises the same document-level cleanup the app relies on.
    document.dispatchEvent(new Event('dragend'))
  })

  function createDragEvent(selectedText: string, target?: Element | null) {
    const dataTransfer = stubDataTransfer()

    // Simulate a live selection
    const selection = {
      isCollapsed: selectedText.length === 0,
      toString: () => selectedText
    }

    vi.spyOn(window, 'getSelection').mockReturnValue(selection as Selection)

    return {
      dataTransfer,
      clientX: 100,
      clientY: 200,
      preventDefault: vi.fn(),
      target: target ?? null
    }
  }

  it('sets text/plain AND the quote MIME on dragstart', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const event = createDragEvent('hello world')

    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)

    expect(event.dataTransfer.effectAllowed).toBe('copy')
    // text/plain for OS interop, HERMES_QUOTE_MIME as the composer's marker
    expect(event.dataTransfer.getData('text/plain')).toBe('> hello world')
    expect(event.dataTransfer.getData(HERMES_QUOTE_MIME)).toBe('> hello world')
  })

  it('quotes each line separately', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const event = createDragEvent('line one\nline two\nline three')

    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)

    expect(event.dataTransfer.getData('text/plain')).toBe('> line one\n> line two\n> line three')
  })

  it('drops a trailing newline before quoting', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const event = createDragEvent('line one\n')

    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)

    expect(event.dataTransfer.getData('text/plain')).toBe('> line one')
  })

  it('does not cancel a native link drag when nothing is selected', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const link = document.createElement('a')
    const event = createDragEvent('', link)

    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)

    // The native link drag must survive: no preventDefault, no ghost, no data.
    expect(event.preventDefault).not.toHaveBeenCalled()
    expect(ghostCount()).toBe(0)
    expect(event.dataTransfer.getData('text/plain')).toBe('')
  })

  it('prevents the drag when no text is selected', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const event = createDragEvent('')

    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)

    expect(event.preventDefault).toHaveBeenCalled()
  })

  it('prevents the drag when selection is collapsed', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const event = createDragEvent('')

    // Collapsed selection: isCollapsed = true, toString returns ''
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: true,
      toString: () => ''
    } as Selection)

    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)

    expect(event.preventDefault).toHaveBeenCalled()
  })

  it('creates a ghost on dragstart and cleans it up on dragend', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const event = createDragEvent('test')

    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)

    expect(ghostCount()).toBe(1)

    result.current.onDragEnd()

    expect(ghostCount()).toBe(0)
  })

  it('shows a line count in the ghost for multi-line selections', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const event = createDragEvent('line one\nline two')

    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)

    const ghost = Array.from(document.body.children).find(
      el => (el as HTMLElement).style.position === 'fixed'
    ) as HTMLElement | undefined

    expect(ghost?.textContent).toContain('2 lines')

    result.current.onDragEnd()
  })

  it('releases the ghost via the document-level dragend listener even after the component unmounts', () => {
    const { result, unmount } = renderHook(() => useDragTextToComposer())
    const event = createDragEvent('survives unmount')

    // Start the drag, then unmount the bubble mid-drag (streaming removes a
    // message). The document-level listener must still release the ghost.
    result.current.onDragStart(event as unknown as React.DragEvent<HTMLElement>)
    expect(ghostCount()).toBe(1)

    unmount()

    // The browser fires dragend on the document when a drag finishes — even
    // when the source node was removed mid-drag.
    document.dispatchEvent(new Event('dragend'))

    expect(ghostCount()).toBe(0)
  })

  it('releases a stale ghost when a new drag starts', () => {
    const { result } = renderHook(() => useDragTextToComposer())
    const first = createDragEvent('first drag')
    const second = createDragEvent('second drag')

    result.current.onDragStart(first as unknown as React.DragEvent<HTMLElement>)
    expect(ghostCount()).toBe(1)

    // A second drag without a dragend in between must not leak the first ghost
    result.current.onDragStart(second as unknown as React.DragEvent<HTMLElement>)
    expect(ghostCount()).toBe(1)

    result.current.onDragEnd()
    expect(ghostCount()).toBe(0)
  })

  it('is a no-op on dragend when no ghost was created', () => {
    const { result } = renderHook(() => useDragTextToComposer())

    // Should not throw
    expect(() => result.current.onDragEnd()).not.toThrow()
  })
})
