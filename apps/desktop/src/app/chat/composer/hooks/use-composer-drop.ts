import { type DragEvent as ReactDragEvent, useRef, useState } from 'react'

import { type ComposerTarget, requestComposerInsert } from '@/app/chat/composer/focus'
import { triggerHaptic } from '@/lib/haptics'

import { extractDroppedFiles, HERMES_PATHS_MIME, HERMES_QUOTE_MIME, partitionDroppedFiles } from '../../hooks/use-composer-actions'
import { dragHasAttachments, droppedFileInlineRefs, type InlineRefInput } from '../inline-refs'
import type { ChatBarProps } from '../types'

/** True when the drag carries a message-bubble quote (HERMES_QUOTE_MIME).
 * Deliberately NOT keyed on `text/plain`: foreign text/plain drags (kanban
 * cards, external apps) must keep their existing behavior untouched. */
const hasQuoteData = (transfer: DataTransfer) => Array.from(transfer.types || []).includes(HERMES_QUOTE_MIME)

interface UseComposerDropArgs {
  cwd: ChatBarProps['cwd']
  insertInlineRefs: (refs: InlineRefInput[]) => boolean
  onAttachDroppedItems: ChatBarProps['onAttachDroppedItems']
  requestMainFocus: () => void
  /** Focus-bus routing key of THIS composer — quote drops land here, never in
   * whatever composer happens to be `'active'` (e.g. an open edit composer). */
  target: ComposerTarget
}

/**
 * Drag-and-drop attachment engine. Splits drops by origin: in-app drags
 * (project tree / gutter) stay inline `@file:`/`@line:` refs the gateway
 * resolves directly; OS/Finder drops (absolute local paths a remote gateway
 * can't read, image bytes vision needs) route through the upload pipeline.
 * Off the keystroke path; consumes `insertInlineRefs` + the attach handler.
 */
export function useComposerDrop({
  cwd,
  insertInlineRefs,
  onAttachDroppedItems,
  requestMainFocus,
  target
}: UseComposerDropArgs) {
  const [dragActive, setDragActive] = useState(false)
  const dragDepthRef = useRef(0)

  const resetDragState = () => {
    dragDepthRef.current = 0
    setDragActive(false)
  }

  const handleDragEnter = (event: ReactDragEvent<HTMLFormElement>) => {
    if (!onAttachDroppedItems && !hasQuoteData(event.dataTransfer)) {
      return
    }

    if (!dragHasAttachments(event.dataTransfer, HERMES_PATHS_MIME) && !hasQuoteData(event.dataTransfer)) {
      return
    }

    event.preventDefault()
    dragDepthRef.current += 1

    if (!dragActive) {
      setDragActive(true)
    }
  }

  const handleDragOver = (event: ReactDragEvent<HTMLFormElement>) => {
    if (!onAttachDroppedItems && !hasQuoteData(event.dataTransfer)) {
      return
    }

    if (!dragHasAttachments(event.dataTransfer, HERMES_PATHS_MIME) && !hasQuoteData(event.dataTransfer)) {
      return
    }

    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
  }

  const handleDragLeave = (event: ReactDragEvent<HTMLFormElement>) => {
    event.preventDefault()
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1)

    if (dragDepthRef.current === 0) {
      setDragActive(false)
    }
  }

  const handleDrop = (event: ReactDragEvent<HTMLFormElement>) => {
    if (!onAttachDroppedItems && !hasQuoteData(event.dataTransfer)) {
      return
    }

    event.preventDefault()
    resetDragState()

    const candidates = extractDroppedFiles(event.dataTransfer)

    if (candidates.length > 0) {
      // In-app drags (project tree / gutter) are workspace-relative paths the
      // gateway resolves directly, so they stay inline @file:/@line: refs. OS
      // drops are absolute local paths a remote gateway can't read (and images
      // need byte upload for vision), so route them through the upload pipeline.
      const { inAppRefs, osDrops } = partitionDroppedFiles(candidates)
      const refs = droppedFileInlineRefs(inAppRefs, cwd)

      if (refs.length && insertInlineRefs(refs)) {
        triggerHaptic('selection')
      }

      if (osDrops.length && onAttachDroppedItems) {
        void Promise.resolve(onAttachDroppedItems(osDrops)).then(attached => {
          if (attached) {
            triggerHaptic('selection')
            requestMainFocus()
          }
        })
      }

      return
    }

    // Quote drop: selected message text dragged into the composer.
    // Insert as a quoted block (same format as "Paste as text"), routed to
    // THIS composer's scope — never to whatever composer is 'active'.
    const text = event.dataTransfer.getData(HERMES_QUOTE_MIME).trim()

    if (text) {
      requestComposerInsert(text, { target })
      triggerHaptic('selection')
    }
  }

  const handleInputDragOver = (event: ReactDragEvent<HTMLDivElement>) => {
    if (!dragHasAttachments(event.dataTransfer, HERMES_PATHS_MIME) && !hasQuoteData(event.dataTransfer)) {
      return
    }

    event.preventDefault()
    event.stopPropagation()
    event.dataTransfer.dropEffect = 'copy'
  }

  const handleInputDrop = (event: ReactDragEvent<HTMLDivElement>) => {
    if (!dragHasAttachments(event.dataTransfer, HERMES_PATHS_MIME) && !hasQuoteData(event.dataTransfer)) {
      return
    }

    const candidates = extractDroppedFiles(event.dataTransfer)

    if (candidates.length > 0) {
      event.preventDefault()
      event.stopPropagation()
      resetDragState()

      // Dropping straight onto the text box used to inline-ref *every* file —
      // including OS/Finder drops, whose absolute local path a remote gateway
      // can't read and whose image bytes never reached vision. Split by origin:
      // in-app drags stay inline refs; OS drops go through the upload pipeline.
      // (When no upload handler is wired, fall back to inline refs for all.)
      const attach = onAttachDroppedItems
      const { inAppRefs, osDrops } = partitionDroppedFiles(candidates)
      const refs = droppedFileInlineRefs(attach ? inAppRefs : candidates, cwd)

      if (refs.length && insertInlineRefs(refs)) {
        triggerHaptic('selection')
      }

      if (attach && osDrops.length) {
        void Promise.resolve(attach(osDrops)).then(attached => {
          if (attached) {
            triggerHaptic('selection')
            requestMainFocus()
          }
        })
      }

      return
    }

    // Quote drop onto the input area, routed to THIS composer's scope.
    const text = event.dataTransfer.getData(HERMES_QUOTE_MIME).trim()

    if (text) {
      event.preventDefault()
      event.stopPropagation()
      resetDragState()
      requestComposerInsert(text, { target })
      triggerHaptic('selection')
    }
  }

  return {
    dragActive,
    handleDragEnter,
    handleDragLeave,
    handleDragOver,
    handleDrop,
    handleInputDragOver,
    handleInputDrop
  }
}
