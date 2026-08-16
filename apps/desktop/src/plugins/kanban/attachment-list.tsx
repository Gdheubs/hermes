import { Codicon, host } from '@hermes/plugin-sdk'

import { useKanban } from './i18n'
import type { KanbanAttachment } from './types'

export function AttachmentList({ attachments }: { attachments: KanbanAttachment[] }) {
  const k = useKanban()

  const preview = async (storedPath: string, filename: string) => {
    let opened = false

    try {
      opened = await host.previewFile(storedPath, filename)
    } catch {
      opened = false
    }

    // A backend-side path may not exist on this machine (remote gateway /
    // Hermes Cloud) — tell the user instead of silently doing nothing.
    if (!opened) {
      host.notify({ kind: 'error', message: k.previewUnavailable(filename) })
    }
  }

  return (
    <ul className="flex flex-col gap-1">
      {attachments.map(attachment => {
        const storedPath = attachment.stored_path

        return (
          <li className="flex items-center gap-1.5 text-[0.75rem] text-(--ui-text-tertiary)" key={attachment.id}>
            <Codicon name="file" size="0.75rem" />
            {storedPath ? (
              <button
                aria-label={k.previewAttachment(attachment.filename)}
                className="min-w-0 truncate text-left underline-offset-2 hover:text-foreground hover:underline"
                onClick={() => void preview(storedPath, attachment.filename)}
                title={attachment.filename}
                type="button"
              >
                {attachment.filename}
              </button>
            ) : (
              <span className="min-w-0 truncate" title={attachment.filename}>
                {attachment.filename}
              </span>
            )}
          </li>
        )
      })}
    </ul>
  )
}
