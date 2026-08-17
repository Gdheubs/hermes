import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useComposerDrop } from '@/app/chat/composer/hooks/use-composer-drop'
import { HERMES_QUOTE_MIME } from '@/app/chat/hooks/use-composer-actions'

// Composer drop-handler tests for the quote-drop path added alongside the
// existing file-drop engine. A drag of selected message text carries the
// HERMES_QUOTE_MIME marker — no HERMES_PATHS_MIME, no Files — and must reach
// the composer as a quoted block via requestComposerInsert, without
// disturbing the file-drop paths that already existed. Foreign text/plain
// drags (kanban cards, external apps) must be ignored entirely.

const insertComposer = vi.hoisted(() => vi.fn())

vi.mock('@/app/chat/composer/focus', () => ({
  requestComposerInsert: insertComposer
}))

// Minimal DataTransfer stand-in for jsdom. `types` is the only field the
// guards read; `getData` serves the drop handlers.
function quoteTransfer(text: string) {
  return {
    dropEffect: 'none',
    effectAllowed: 'none',
    files: { length: 0, item: () => null },
    getData: (format: string) => (format === HERMES_QUOTE_MIME ? text : ''),
    items: [] as unknown[],
    types: text.length > 0 ? [HERMES_QUOTE_MIME] : []
  }
}

/** A foreign text/plain drag (kanban card id, external app text) — carries
 * NO HERMES_QUOTE_MIME and must be left alone. */
function foreignTextTransfer(text: string) {
  return {
    dropEffect: 'none',
    effectAllowed: 'none',
    files: { length: 0, item: () => null },
    getData: (format: string) => (format === 'text/plain' ? text : ''),
    items: [] as unknown[],
    types: ['text/plain']
  }
}

function renderDrop() {
  const insertInlineRefs = vi.fn(() => false)
  const onAttachDroppedItems = vi.fn()
  const requestMainFocus = vi.fn()

  const { result } = renderHook(() =>
    useComposerDrop({
      cwd: '/repo',
      insertInlineRefs,
      onAttachDroppedItems,
      requestMainFocus,
      target: 'main'
    })
  )

  return { insertInlineRefs, onAttachDroppedItems, requestMainFocus, result }
}

describe('useComposerDrop — quote drops', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    insertComposer.mockReset()
  })

  it('inserts dragged text into the composer as-is (pre-quoted by the drag source)', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: quoteTransfer('> hello world'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleDrop(event as unknown as React.DragEvent<HTMLFormElement>)

    expect(insertComposer).toHaveBeenCalledWith('> hello world', { target: 'main' })
    expect(event.preventDefault).toHaveBeenCalled()
  })

  it('trims surrounding whitespace from the dropped text', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: quoteTransfer('  > hello world\n\n  '),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleDrop(event as unknown as React.DragEvent<HTMLFormElement>)

    expect(insertComposer).toHaveBeenCalledWith('> hello world', { target: 'main' })
  })

  it('ignores an empty quote drop', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: quoteTransfer(''),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleDrop(event as unknown as React.DragEvent<HTMLFormElement>)

    expect(insertComposer).not.toHaveBeenCalled()
  })

  it('ignores a foreign text/plain drop (kanban card, external app text)', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: foreignTextTransfer('task-123'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleDrop(event as unknown as React.DragEvent<HTMLFormElement>)

    // The quote never reaches the composer. (preventDefault alone matches the
    // pre-existing file-drop path: with an attach handler wired, any drop is
    // swallowed — what matters is that no insertion happens.)
    expect(insertComposer).not.toHaveBeenCalled()
  })

  it('ignores a foreign text/plain drag-over on the form', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: foreignTextTransfer('task-123'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleDragOver(event as unknown as React.DragEvent<HTMLFormElement>)

    expect(event.preventDefault).not.toHaveBeenCalled()
  })

  it('inserts text dropped onto the input area', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: quoteTransfer('> from input drop'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleInputDrop(event as unknown as React.DragEvent<HTMLDivElement>)

    expect(insertComposer).toHaveBeenCalledWith('> from input drop', { target: 'main' })
    expect(event.stopPropagation).toHaveBeenCalled()
  })

  it('quote drag-over accepts the drop with copy effect on the form', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: quoteTransfer('> hello'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleDragOver(event as unknown as React.DragEvent<HTMLFormElement>)

    expect(event.preventDefault).toHaveBeenCalled()
    expect(event.dataTransfer.dropEffect).toBe('copy')
  })

  it('quote drag-over accepts the drop on the input area', () => {
    const { result } = renderDrop()

    const event = {
      dataTransfer: quoteTransfer('> hello'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleInputDragOver(event as unknown as React.DragEvent<HTMLDivElement>)

    expect(event.preventDefault).toHaveBeenCalled()
    expect(event.stopPropagation).toHaveBeenCalled()
    expect(event.dataTransfer.dropEffect).toBe('copy')
  })

  it('quote drops work even when no attach handler is wired', () => {
    const insertInlineRefs = vi.fn(() => false)
    const requestMainFocus = vi.fn()

    const { result } = renderHook(() =>
      useComposerDrop({
        cwd: '/repo',
        insertInlineRefs,
        onAttachDroppedItems: undefined,
        requestMainFocus,
        target: 'main'
      })
    )

    const event = {
      dataTransfer: quoteTransfer('> still works'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleDrop(event as unknown as React.DragEvent<HTMLFormElement>)

    expect(insertComposer).toHaveBeenCalledWith('> still works', { target: 'main' })
  })

  it('routes quote drops to this composer\u2019s own scope, not the active composer', () => {
    const insertInlineRefs = vi.fn(() => false)
    const requestMainFocus = vi.fn()

    const { result } = renderHook(() =>
      useComposerDrop({
        cwd: '/repo',
        insertInlineRefs,
        onAttachDroppedItems: undefined,
        requestMainFocus,
        target: 'tile:abc'
      })
    )

    const event = {
      dataTransfer: quoteTransfer('> tile quote'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleDrop(event as unknown as React.DragEvent<HTMLFormElement>)

    // requestComposerInsert was called with an explicit { target } — the
    // mocked focus bus records it. The second arg must be { target: 'tile:abc' }.
    expect(insertComposer).toHaveBeenCalledWith('> tile quote', { target: 'tile:abc' })
  })

  it('routes input-area quote drops to this composer\u2019s own scope', () => {
    const insertInlineRefs = vi.fn(() => false)
    const requestMainFocus = vi.fn()

    const { result } = renderHook(() =>
      useComposerDrop({
        cwd: '/repo',
        insertInlineRefs,
        onAttachDroppedItems: undefined,
        requestMainFocus,
        target: 'tile:abc'
      })
    )

    const event = {
      dataTransfer: quoteTransfer('> input quote'),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    result.current.handleInputDrop(event as unknown as React.DragEvent<HTMLDivElement>)

    expect(insertComposer).toHaveBeenCalledWith('> input quote', { target: 'tile:abc' })
  })

  it('does not throw when a mixed files+quote drag arrives with no attach handler wired', () => {
    const insertInlineRefs = vi.fn(() => false)
    const requestMainFocus = vi.fn()

    const { result } = renderHook(() =>
      useComposerDrop({
        cwd: '/repo',
        insertInlineRefs,
        onAttachDroppedItems: undefined,
        requestMainFocus,
        target: 'main'
      })
    )

    // A drag carrying BOTH a file and the quote MIME: the quote guard lets it
    // through the top-level check, the file branch runs, but there is no
    // attach handler to hand osDrops to. Regression guard for the TypeError
    // that used to fire on `onAttachDroppedItems!(osDrops)`.
    const event = {
      dataTransfer: {
        dropEffect: 'none',
        effectAllowed: 'none',
        files: { length: 1, item: () => new File(['x'], 'notes.txt') },
        getData: (format: string) => (format === HERMES_QUOTE_MIME ? '> text' : ''),
        items: [{ kind: 'file', getAsFile: () => new File(['x'], 'notes.txt'), webkitGetAsEntry: () => null }],
        types: ['Files', HERMES_QUOTE_MIME]
      },
      preventDefault: vi.fn(),
      stopPropagation: vi.fn()
    }

    expect(() => result.current.handleDrop(event as unknown as React.DragEvent<HTMLFormElement>)).not.toThrow()
  })
})
