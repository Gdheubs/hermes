import { useCallback } from 'react'

import { HERMES_QUOTE_MIME } from '@/app/chat/hooks/use-composer-actions'
import { createDragGhost, type DragGhost } from '@/lib/drag-ghost'

import { hasTextSelection } from './selection'

/**
 * Shared drag-text-to-composer behavior for message bubbles (user + assistant).
 *
 * When the user selects text in a message and starts dragging, the handler:
 * 1. Reads the live selection text
 * 2. Formats it as a quoted block ("> line" per line, same as "Paste as text")
 * 3. Sets `text/plain` (OS interop) AND the HERMES_QUOTE_MIME marker on the
 *    DataTransfer — the composer's drop handler (use-composer-drop) accepts
 *    quote drops only when the marker is present, so foreign text/plain
 *    drags (kanban cards, external apps) keep their existing behavior
 * 4. Creates a drag ghost via the existing `createDragGhost` utility
 *
 * The ghost is destroyed on `dragend`. Cleanup is module-level (not a ref
 * inside the hook) on purpose: message bubbles can unmount mid-drag (a
 * streaming turn removes a message, a session switch tears the transcript
 * down), and a ref owned by the unmounted component would orphan the ghost
 * node forever. The module holder + document-level dragend listener survive
 * unmount and always release the node.
 *
 * This is an ADDITION, not a replacement: dragstart is a separate event from
 * click, contextmenu, and pointerdown. No existing behavior is touched.
 */

let activeGhost: DragGhost | null = null

function releaseGhost() {
  activeGhost?.destroy()
  activeGhost = null
}

function ensureDocumentListener() {
  if (typeof document === 'undefined') {
    return
  }

  // Idempotent: a single document-level dragend listener serves every message
  // bubble. It also catches drags that end OUTSIDE the app window, where the
  // bubble's own React dragend handler may never fire.
  if (document.documentElement.dataset.hermesDragGhostArmed === '1') {
    return
  }

  document.documentElement.dataset.hermesDragGhostArmed = '1'
  document.addEventListener('dragend', releaseGhost)
}

ensureDocumentListener()

export function useDragTextToComposer() {
  const onDragStart = useCallback((event: React.DragEvent<HTMLElement>) => {
    // Natively draggable children (links, images) own their drag: never cancel
    // it, even without a text selection — pre-commit those dragged out fine
    // and the native drop inserts at the caret. Only a text-selection drag is
    // ours to take over.
    if (!hasTextSelection()) {
      if (event.target instanceof Element && event.target.closest('a, img')) {
        return
      }

      event.preventDefault()

      return
    }

    const selection = window.getSelection()
    const text = selection?.toString().trim() ?? ''

    if (!text) {
      event.preventDefault()

      return
    }

    // A trailing newline in the selection would otherwise quote as a dangling
    // "> " line; drop it before quoting.
    const lines = text.split('\n')

    if (lines[lines.length - 1] === '') {
      lines.pop()
    }

    const quoted = lines.map(line => `> ${line}`).join('\n')

    event.dataTransfer.effectAllowed = 'copy'
    // text/plain keeps the drag interop with OS-level targets; HERMES_QUOTE_MIME
    // is the marker the composer's drop handler keys on, so foreign text/plain
    // drags (kanban cards, external apps) keep their existing behavior.
    event.dataTransfer.setData('text/plain', quoted)
    event.dataTransfer.setData(HERMES_QUOTE_MIME, quoted)

    // Drag ghost: a flat, pointer-following chip showing what's being dragged.
    // Truncate the preview to ~40 chars; multi-line selections get a line
    // count so a selection that spans message bubbles is identifiable at a
    // glance (the payload is the live document selection — what-you-see-is-
    // what-you-drag, matching native Chromium drag-text behavior).
    releaseGhost()
    const preview = text.length > 40 ? text.slice(0, 40) + '…' : text
    const label = lines.length > 1 ? `${preview} (${lines.length} lines)` : preview
    activeGhost = createDragGhost(label)
    activeGhost.moveTo(event.clientX, event.clientY)
  }, [])

  const onDragEnd = useCallback(() => {
    releaseGhost()
  }, [])

  return { onDragStart, onDragEnd }
}
