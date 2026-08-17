import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'
import { $activeSessionId, $currentModel, $currentProvider, setCurrentModel, setCurrentProvider } from '@/store/session'

import { SessionSurfaceChat } from './session-surface-chat'

const requestGatewayForProfile = vi.fn(async (..._args: unknown[]) => ({}))

vi.mock('@/store/gateway', () => ({
  profileGatewayState: () => ({ connectionState: 'open' }),
  subscribeProfileGateway: () => () => undefined,
  requestGatewayForProfile: (
    profile: string,
    method: string,
    params?: Record<string, unknown>,
    timeout?: number,
    signal?: AbortSignal
  ) => requestGatewayForProfile(profile, method, params, timeout, signal)
}))

vi.mock('@/app/shell/model-menu-panel', () => ({
  ModelMenuPanel: ({ onSelectModel }: { onSelectModel: (selection: unknown) => Promise<boolean> }) => (
    <button
      onClick={() =>
        void onSelectModel({ model: 'claude-sonnet-4.6', provider: 'anthropic', sessionId: 'shared-runtime' })
      }
      type="button"
    >
      Select surface model
    </button>
  )
}))

vi.mock('./hooks/use-composer-actions', () => ({
  useComposerActions: () => ({
    addContextRefAttachment: vi.fn(),
    attachDroppedItems: vi.fn(),
    attachImageBlob: vi.fn(),
    attachPrCommentUrl: vi.fn(),
    pasteClipboardImage: vi.fn(),
    pickContextPaths: vi.fn(),
    pickImages: vi.fn(),
    removeAttachment: vi.fn()
  })
}))

vi.mock('./session-tile-actions', () => ({
  useSessionTileActions: () => ({
    cancelRun: vi.fn(),
    dismissError: vi.fn(),
    editMessage: vi.fn(),
    handleThreadMessagesChange: vi.fn(),
    reloadFromMessage: vi.fn(),
    restoreToMessage: vi.fn(),
    steerPrompt: vi.fn(),
    submitText: vi.fn()
  })
}))

vi.mock('.', () => ({
  ChatView: ({ modelMenuContent }: { modelMenuContent?: React.ReactNode }) => <div>{modelMenuContent}</div>
}))

describe('SessionSurface model control', () => {
  beforeEach(() => {
    requestGatewayForProfile.mockClear()
    $activeGatewayProfile.set('profile-a')
    $activeSessionId.set('shared-runtime')
    setCurrentModel('foreground-model')
    setCurrentProvider('foreground-provider')
  })

  it('selects through the owner profile requester without mutating the colliding foreground', async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SessionSurfaceChat profile="profile-b" runtimeSessionId="shared-runtime" storedSessionId="stored-b" />
      </QueryClientProvider>
    )

    fireEvent.click(screen.getByRole('button', { name: 'Select surface model' }))

    await waitFor(() =>
      expect(requestGatewayForProfile).toHaveBeenCalledWith(
        'profile-b',
        'config.set',
        {
          key: 'model',
          profile: 'profile-b',
          session_id: 'shared-runtime',
          value: 'claude-sonnet-4.6 --provider anthropic --session'
        },
        undefined,
        undefined
      )
    )
    expect($currentModel.get()).toBe('foreground-model')
    expect($currentProvider.get()).toBe('foreground-provider')
  })
})
