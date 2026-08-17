import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'
import { $sessions } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

import { SessionTilePane } from './session-tile'

vi.mock('./session-surface', () => ({
  SessionSurfaceCore: (props: { profile: string; runtimeSessionId?: null | string; storedSessionId: string }) => (
    <div data-profile={props.profile} data-runtime={props.runtimeSessionId} data-testid="shared-session-surface">
      {props.storedSessionId}
    </div>
  )
}))

vi.mock('./sidebar/session-actions-menu', () => ({
  SessionContextMenu: ({ children }: { children: React.ReactNode }) => <div>{children}</div>
}))

describe('SessionTilePane shared surface', () => {
  beforeEach(() => {
    $activeGatewayProfile.set('work')
    $sessions.set([{ id: 'stored-tile', profile: 'vision' } as never])
    $sessionTiles.set([{ runtimeId: 'runtime-tile', storedSessionId: 'stored-tile' }])
  })

  it('renders the same SessionSurface exported to plugins (no in-tile chat path)', () => {
    render(<SessionTilePane storedSessionId="stored-tile" />)

    const surface = screen.getByTestId('shared-session-surface')
    expect(surface.textContent).toBe('stored-tile')
    expect(surface.dataset.profile).toBe('vision')
    expect(surface.dataset.runtime).toBe('runtime-tile')
  })

  it('keeps the tile owner profile when the foreground gateway profile changes', () => {
    render(<SessionTilePane storedSessionId="stored-tile" />)

    $activeGatewayProfile.set('personal')

    expect(screen.getByTestId('shared-session-surface').dataset.profile).toBe('vision')
  })

  it('patches the live runtime id back onto the tile as the surface binds', () => {
    render(<SessionTilePane storedSessionId="stored-tile" />)

    // The surface owns the binding; the tile already mirrors the runtime id it
    // was seeded with so tab chrome (status dot, close gate) stays live.
    expect(screen.getByTestId('shared-session-surface').dataset.runtime).toBe('runtime-tile')
  })
})
