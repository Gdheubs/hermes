import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { useRef, useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $composerEnterSends, setComposerEnterSends } from '@/store/composer-enter'

// No global setupFiles registers auto-cleanup, so unmount between tests.
afterEach(cleanup)

// Faithful mirror of index.tsx's Enter wiring (handleEditorKeyDown's Enter
// branch + submitDraft), driven through REAL DOM keydown events on a
// contentEditable — same shape as enter-submit-dom-race.test.tsx.
//
// The swap is driven by the real $composerEnterSends persistentAtom, read
// imperatively inside the handler exactly as index.tsx does, so these tests
// exercise the store + handler integration, not a copy of the predicate.
// For the new-line case we dispatch a cancelable native KeyboardEvent and
// assert defaultPrevented stays false — that is the invariant that lets the
// contentEditable keep its native newline.
function Harness({
  busy = false,
  disabled = false,
  queued = [],
  onSubmit,
  onQueue,
  onCancel,
  onDrain,
  onSendNow
}: {
  busy?: boolean
  disabled?: boolean
  queued?: readonly string[]
  onSubmit: (text: string) => void
  onQueue: (text: string) => void
  onCancel: () => void
  onDrain: () => void
  onSendNow?: (id: string) => void
}) {
  const editorRef = useRef<HTMLDivElement>(null)
  const draftRef = useRef('')
  // Mirrors `useAuiState(s => s.composer.text)` — updated only via setText, so
  // it lags the DOM until React re-renders (the source of the race).
  const [draft, setDraft] = useState('')
  const attachments: unknown[] = []

  const composerPlainText = (el: HTMLElement) => el.textContent ?? ''

  const setText = (next: string) => {
    draftRef.current = next
    setDraft(next)
  }

  const submitDraft = () => {
    if (disabled) {
      return
    }

    const editor = editorRef.current

    if (editor) {
      const domText = composerPlainText(editor)

      if (domText !== draftRef.current) {
        draftRef.current = domText
        setDraft(domText)
      }
    }

    const text = draftRef.current
    const payloadPresent = text.trim().length > 0 || attachments.length > 0

    if (busy) {
      if (payloadPresent) {
        onQueue(text)
      } else {
        onCancel()
      }
    } else if (!payloadPresent && queued.length > 0) {
      onDrain()
    } else if (payloadPresent) {
      onSubmit(text)
    }
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.shiftKey) {
      event.preventDefault()

      if (busy && !disabled) {
        const editorText = editorRef.current ? composerPlainText(editorRef.current) : draftRef.current
        queueDraft(editorText)
      }

      return
    }

    // Verbatim mirror of index.tsx's Enter swap.
    const composerEnterSends = $composerEnterSends.get()
    const enterSends = composerEnterSends ? !event.shiftKey : event.shiftKey

    if (event.key === 'Enter' && enterSends) {
      event.preventDefault()

      const editorText = editorRef.current ? composerPlainText(editorRef.current) : draftRef.current
      const hasLivePayload = editorText.trim().length > 0 || attachments.length > 0

      if (disabled) {
        return
      }

      if (!busy && !hasLivePayload && queued.length > 0) {
        onDrain()

        return
      }

      if (busy && !hasLivePayload) {
        const head = queued[0]

        if (head) {
          onSendNow?.(head)
        }

        return
      }

      submitDraft()
    }
  }

  const queueDraft = (text: string) => {
    draftRef.current = text
    onQueue(text)
  }

  return (
    <div
      contentEditable
      data-testid="editor"
      onInput={event => setText(composerPlainText(event.currentTarget))}
      onKeyDown={handleKeyDown}
      ref={editorRef}
      suppressContentEditableWarning
    />
  )
}

describe('composer Enter-key behavior (+$composerEnterSends)', () => {
  it('default (setting on): a plain Enter submits', async () => {
    setComposerEnterSends(true)
    const onSubmit = vi.fn()

    const { getByTestId } = render(
      <Harness onCancel={vi.fn()} onDrain={vi.fn()} onQueue={vi.fn()} onSubmit={onSubmit} />
    )

    const editor = getByTestId('editor')

    await act(async () => {
      editor.textContent = 'hello'
      fireEvent.keyDown(editor, { key: 'Enter' })
    })

    expect(onSubmit).toHaveBeenCalledWith('hello')
  })

  it('default (setting on): Shift+Enter does NOT send (it is the native newline)', async () => {
    setComposerEnterSends(true)
    const onSubmit = vi.fn()

    const { getByTestId } = render(
      <Harness onCancel={vi.fn()} onDrain={vi.fn()} onQueue={vi.fn()} onSubmit={onSubmit} />
    )

    const editor = getByTestId('editor')

    await act(async () => {
      editor.textContent = 'hello'
      fireEvent.keyDown(editor, { key: 'Enter', shiftKey: true })
    })

    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('new-line mode: a plain Enter does NOT preventDefault and does NOT send', async () => {
    setComposerEnterSends(false)
    const onSubmit = vi.fn()

    const { getByTestId } = render(
      <Harness onCancel={vi.fn()} onDrain={vi.fn()} onQueue={vi.fn()} onSubmit={onSubmit} />
    )

    const editor = getByTestId('editor')

    let prevented = false

    await act(async () => {
      editor.textContent = 'hello'

      // Detect prevention on a real, cancelable KeyboardEvent so we assert the
      // contentEditable keeps its native newline (no preventDefault).
      const native = new KeyboardEvent('keydown', {
        bubbles: true,
        cancelable: true,
        key: 'Enter'
      })

      editor.dispatchEvent(native)
      prevented = native.defaultPrevented
    })

    expect(prevented).toBe(false)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('new-line mode: Shift+Enter submits', async () => {
    setComposerEnterSends(false)
    const onSubmit = vi.fn()

    const { getByTestId } = render(
      <Harness onCancel={vi.fn()} onDrain={vi.fn()} onQueue={vi.fn()} onSubmit={onSubmit} />
    )

    const editor = getByTestId('editor')

    await act(async () => {
      editor.textContent = 'hello'
      fireEvent.keyDown(editor, { key: 'Enter', shiftKey: true })
    })

    expect(onSubmit).toHaveBeenCalledWith('hello')
  })

  it('new-line mode: Cmd/Ctrl+Enter still queues a follow-up while busy (untouched)', async () => {
    setComposerEnterSends(false)
    const onQueue = vi.fn()

    const { getByTestId } = render(
      <Harness busy onCancel={vi.fn()} onDrain={vi.fn()} onQueue={onQueue} onSubmit={vi.fn()} />
    )

    const editor = getByTestId('editor')

    await act(async () => {
      editor.textContent = 'follow-up'
      fireEvent.keyDown(editor, { key: 'Enter', metaKey: true })
    })

    expect(onQueue).toHaveBeenCalledWith('follow-up')
  })
})
