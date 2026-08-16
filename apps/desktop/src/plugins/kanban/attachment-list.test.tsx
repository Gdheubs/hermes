import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const { messages, notify, previewFile } = vi.hoisted(() => ({
  messages: { en: undefined as Record<string, unknown> | undefined },
  notify: vi.fn(),
  previewFile: vi.fn()
}))

vi.mock('@hermes/plugin-sdk', () => ({
  Codicon: ({ name }: { name: string }) => <span>{name}</span>,
  host: { notify, previewFile },
  // Resolves against the plugin's real en bundle (wired below, after module
  // load) so the strings under test are the shipped ones.
  usePluginI18n:
    () =>
    (key: string, ...args: unknown[]) => {
      const value = key
        .split('.')
        .reduce<unknown>((node, part) => (node as Record<string, unknown>)?.[part], messages.en)

      return typeof value === 'function' ? value(...args) : String(value ?? key)
    }
}))

import { AttachmentList } from './attachment-list'
import { KANBAN_LOCALES } from './i18n'

messages.en = KANBAN_LOCALES.en

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Kanban attachment list', () => {
  it('opens an attachment in the shared preview pane', async () => {
    previewFile.mockResolvedValue(true)

    render(
      <AttachmentList
        attachments={[
          {
            id: 8,
            filename: 'evidence.png',
            stored_path: '/tmp/evidence.png'
          }
        ]}
      />
    )

    fireEvent.click(screen.getByRole('button', { name: 'Preview evidence.png' }))

    expect(previewFile).toHaveBeenCalledWith('/tmp/evidence.png', 'evidence.png')

    await waitFor(() => expect(notify).not.toHaveBeenCalled())
  })

  it('surfaces an error when the path cannot be previewed on this machine', async () => {
    // Remote/cloud backends serialize a backend-host path the desktop cannot
    // resolve; previewFile returns false instead of opening anything.
    previewFile.mockResolvedValue(false)

    render(<AttachmentList attachments={[{ id: 8, filename: 'evidence.png', stored_path: '/remote/evidence.png' }]} />)

    fireEvent.click(screen.getByRole('button', { name: 'Preview evidence.png' }))

    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith({
        kind: 'error',
        message: 'Cannot preview evidence.png — the file is not reachable from this machine.'
      })
    )
  })

  it('surfaces an error when the preview call rejects', async () => {
    previewFile.mockRejectedValue(new Error('ipc failure'))

    render(<AttachmentList attachments={[{ id: 8, filename: 'evidence.png', stored_path: '/tmp/evidence.png' }]} />)

    fireEvent.click(screen.getByRole('button', { name: 'Preview evidence.png' }))

    await waitFor(() => expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'error' })))
  })

  it('leaves legacy attachments without a stored path as plain text', () => {
    render(<AttachmentList attachments={[{ id: 9, filename: 'legacy.csv' }]} />)

    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.getByText('legacy.csv')).toBeTruthy()
  })
})
