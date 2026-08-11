import { type ConnectionState, type GatewayEvent, isGatewayReauthRequired, resolveGatewayWsUrl } from '@hermes/shared'
import { atom } from 'nanostores'

import { HermesGateway } from '@/hermes'
import { reconnectBackoffDelayMs } from '@/lib/reconnect-backoff'
import { markNativeNotifyBaseline } from '@/store/notify-baseline'
import { setGatewayState } from '@/store/session'

// ── Multi-profile gateway routing ──────────────────────────────────────────
// Concurrent sessions across profiles need concurrent sockets: the renderer's
// event handler is already session-keyed, so the only thing stopping two
// profiles streaming at once was the single swapping socket. We keep that one
// socket as the PRIMARY (window) backend — owned by use-gateway-boot, with all
// its boot-progress / sleep-wake machinery — and add one persistent SECONDARY
// socket per *other* profile that has live work. Every socket feeds the same
// handleGatewayEvent, so background sessions keep painting. Single-profile users
// only ever have the primary, so their path is byte-for-byte unchanged.

const normKey = (profile: string | null | undefined): string => (profile ?? '').trim() || 'default'

// Read connection state through a call so TS control-flow analysis doesn't
// narrow the getter to a constant across guards (it genuinely changes).
const isOpen = (gateway: HermesGateway | null): boolean => gateway?.connectionState === 'open'

interface RegistryConfig {
  onEvent: (event: GatewayEvent) => void
}

// ── Secondary (pool) backends ──────────────────────────────────────────────
interface Secondary {
  profile: string
  gateway: HermesGateway
  commandLeases: number
  offEvent: () => void
  offState: () => void
  openPromise: Promise<void> | null
  reconnectTimer: ReturnType<typeof setTimeout> | null
  reconnectAttempt: number
  reconnecting: Promise<void> | null
  // While true the entry auto-reconnects on drop; pruning flips it off so a
  // deliberate close doesn't trigger the backoff loop.
  wantOpen: boolean
}

// ── HMR-stable module state ─────────────────────────────────────────────────
// All mutable singletons (live sockets, active-profile routing, the event
// registry) live in ONE container parked on globalThis, NOT in module-level
// `let`/`const` bindings. Reason: this module is imported widely without an HMR
// boundary that accepts it, so editing it (or anything that fans out to it)
// makes Vite issue a FULL PAGE RELOAD — which would kill every live socket and
// drop the agent session on an unrelated edit. Persisting the state on
// globalThis + self-accepting HMR (bottom of file) turns that full reload into
// an in-place hot update that preserves the sockets. Production strips
// import.meta.hot, and a fresh page realm starts with an empty container, so the
// runtime behavior is identical to plain module state.
interface GatewayRegistryState {
  config: RegistryConfig | null
  primaryGateway: HermesGateway | null
  primaryProfile: string
  activeKey: string
  secondaries: Map<string, Secondary>
  primaryReconnect: { gateway: HermesGateway; profile: string; promise: Promise<void> } | null
  $gateway: ReturnType<typeof atom<HermesGateway | null>>
}

const STATE_KEY = Symbol.for('hermes.desktop.gatewayRegistryState')

function createRegistryState(): GatewayRegistryState {
  return {
    config: null,
    primaryGateway: null,
    primaryProfile: 'default',
    activeKey: 'default',
    secondaries: new Map<string, Secondary>(),
    primaryReconnect: null,
    // The active gateway instance, exposed for inline message-stream
    // components (inline ClarifyTool, model overlays) that call gateway
    // methods without the instance threaded down through props.
    $gateway: atom<HermesGateway | null>(null)
  }
}

// Dev only: park the singletons on globalThis so an HMR re-eval of this module
// (self-accepted at the bottom) hands back the SAME live sockets/atoms instead
// of resetting them — that's what keeps the agent session alive across UI edits.
// `import.meta.hot` is undefined in production, so Vite dead-code-eliminates the
// entire globalThis branch and prod uses a plain module-local singleton — no
// globalThis, no Symbol.for. Both realms load the module once, so the container's
// shape and lifetime are identical either way.
function gatewayState(): GatewayRegistryState {
  if (import.meta.hot) {
    const store = globalThis as unknown as { [STATE_KEY]?: GatewayRegistryState }
    store[STATE_KEY] ??= createRegistryState()

    return store[STATE_KEY]
  }

  return createRegistryState()
}

const g = gatewayState()

// Keep HMR-surviving entries compatible when this module's registry shape grows.
g.primaryReconnect ??= null

for (const entry of g.secondaries.values()) {
  entry.commandLeases ??= 0
  entry.openPromise ??= null

  if (typeof entry.reconnecting === 'boolean') {
    entry.reconnecting = null
  }
}

// Re-exported as a stable binding: the atom instance lives in `g`, so every hot
// reload of this module hands back the SAME atom subscribers are already wired
// to. (A fresh `atom()` per reload would orphan existing subscriptions.)
export const $gateway = g.$gateway

export function configureGatewayRegistry(cfg: RegistryConfig): void {
  g.config = cfg
}

/**
 * Feed a synthetic event through the exact same fan-out a real socket frame
 * takes (`config.onEvent` → the desktop's `handleGatewayEvent`). Used by
 * dev-only tooling to exercise the real event branches (e.g. the credit-notice
 * demo) without a backend that can produce the event on demand. No-op until a
 * registry is configured.
 */
export function emitLocalGatewayEvent(event: GatewayEvent): void {
  g.config?.onEvent(event)
}

export function setPrimaryGateway(gateway: HermesGateway | null, profile = 'default'): void {
  g.primaryGateway = gateway
  g.primaryProfile = normKey(profile)
}

export function isActivePrimary(): boolean {
  return g.activeKey === g.primaryProfile
}

export function activeGateway(): HermesGateway | null {
  if (g.activeKey === g.primaryProfile) {
    return g.primaryGateway
  }

  return g.secondaries.get(g.activeKey)?.gateway ?? g.primaryGateway
}

// Mirror a backend's connection state into the global composer state, but only
// when that backend is the one the user is currently looking at. Lets the
// composer reflect the active profile's socket without a background reconnect
// flipping the foreground enabled/disabled state.
function reportGatewayState(profile: string, state: ConnectionState): void {
  // Any socket opening replays parked prompts; hold OS notifications so a
  // launch/reconnect doesn't alert about state that already existed.
  if (state === 'open') {
    markNativeNotifyBaseline()
  }

  if (normKey(profile) === g.activeKey) {
    setGatewayState(state)
  }
}

export function reportPrimaryGatewayState(state: ConnectionState): void {
  reportGatewayState(g.primaryProfile, state)
}

function setActive(profile: string): void {
  g.activeKey = normKey(profile)
  const gateway = activeGateway()
  g.$gateway.set(gateway)
  setGatewayState(gateway?.connectionState ?? 'closed')
}

function clearTimer(entry: Secondary): void {
  if (entry.reconnectTimer !== null) {
    clearTimeout(entry.reconnectTimer)
    entry.reconnectTimer = null
  }
}

async function openSecondary(entry: Secondary): Promise<void> {
  if (entry.openPromise) {
    return entry.openPromise
  }

  const attempt = (async () => {
    const desktop = window.hermesDesktop

    if (!desktop) {
      return
    }

    const conn = await desktop.getConnection(entry.profile)
    const wsUrl = await resolveGatewayWsUrl(desktop, conn)

    // Profile switches and teardown can run while descriptor/ticket lookup
    // awaits. Never resurrect an entry that was already disposed or replaced.
    if (!entry.wantOpen || g.secondaries.get(entry.profile) !== entry) {
      return
    }

    await entry.gateway.connect(wsUrl)

    if (entry.wantOpen && g.secondaries.get(entry.profile) === entry) {
      void desktop.touchBackend?.(entry.profile).catch(() => undefined)
    }
  })()

  entry.openPromise = attempt

  try {
    await attempt
  } finally {
    if (entry.openPromise === attempt) {
      entry.openPromise = null
    }
  }
}

function scheduleReconnect(entry: Secondary): void {
  if (entry.reconnecting || entry.reconnectTimer !== null || !entry.wantOpen) {
    return
  }

  // Full-jitter exponential backoff — same shape (and same reason: avoid a
  // reconnect storm against a restarting gateway) as the primary's.
  const delay = reconnectBackoffDelayMs(entry.reconnectAttempt)
  entry.reconnectAttempt += 1
  entry.reconnectTimer = setTimeout(() => {
    entry.reconnectTimer = null
    void reconnectSecondary(entry)
  }, delay)
}

async function reconnectSecondary(entry: Secondary): Promise<void> {
  if (!entry.wantOpen || isOpen(entry.gateway)) {
    return
  }

  if (entry.reconnecting) {
    return entry.reconnecting
  }

  const reconnecting = (async () => {
    try {
      await openSecondary(entry)
      entry.reconnectAttempt = 0
    } catch {
      // Transport failure → fall through to the backoff below.
    }
  })()

  entry.reconnecting = reconnecting

  try {
    await reconnecting
  } finally {
    if (entry.reconnecting === reconnecting) {
      entry.reconnecting = null
    }

    if (entry.wantOpen && !isOpen(entry.gateway)) {
      scheduleReconnect(entry)
    }
  }
}

function createSecondary(profile: string): Secondary {
  const gateway = new HermesGateway()

  const entry: Secondary = {
    profile,
    gateway,
    commandLeases: 0,
    offEvent: () => {},
    offState: () => {},
    openPromise: null,
    reconnectTimer: null,
    reconnectAttempt: 0,
    reconnecting: null,
    wantOpen: true
  }

  entry.offEvent = gateway.onEvent(event => g.config?.onEvent({ ...event, profile }))
  entry.offState = gateway.onState(state => {
    reportGatewayState(profile, state)

    if (state === 'open') {
      entry.reconnectAttempt = 0
      clearTimer(entry)
    } else if ((state === 'closed' || state === 'error') && entry.wantOpen) {
      scheduleReconnect(entry)
    }
  })

  g.secondaries.set(profile, entry)

  return entry
}

// Open `profile`'s socket WITHOUT making it active — the hover-intent pre-warm
// (store/profile). Runs the same spawn + connect chain as a real switch, so by
// click time ensureGatewayForProfile finds an open socket and just activates
// it. No scheduleReconnect on failure: a hover is speculative, so a dead
// backend must not start a background retry loop — the real switch owns retry
// and error UX. An already-open (or primary) profile is a no-op.
export async function openGatewayForProfile(profile: string): Promise<void> {
  const key = normKey(profile)

  if (key === g.primaryProfile) {
    return
  }

  const entry = g.secondaries.get(key) ?? createSecondary(key)
  entry.wantOpen = true

  if (!isOpen(entry.gateway)) {
    await openSecondary(entry)
  }
}

// Make `profile` the active gateway, lazily opening its socket if needed. The
// primary is a no-op fast path. Background sockets are never closed here.
export async function ensureGatewayForProfile(profile: string): Promise<void> {
  const key = normKey(profile)

  if (key === g.primaryProfile) {
    setActive(key)

    return
  }

  let entry = g.secondaries.get(key)

  if (!entry) {
    entry = createSecondary(key)
  }

  entry.wantOpen = true

  if (!isOpen(entry.gateway)) {
    clearTimer(entry)
    entry.reconnectAttempt = 0

    try {
      await openSecondary(entry)
    } catch {
      scheduleReconnect(entry)
    }
  }

  setActive(key)
}

// Reconnect the active gateway after a transient request failure. Primary
// reconnects are owned by use-gateway-boot, so we only drive secondaries here.
export async function ensureActiveGatewayOpen(): Promise<HermesGateway | null> {
  if (g.activeKey === g.primaryProfile) {
    return g.primaryGateway
  }

  const entry = g.secondaries.get(g.activeKey)

  if (!entry) {
    return null
  }

  if (!isOpen(entry.gateway)) {
    await reconnectSecondary(entry)
  }

  return isOpen(entry.gateway) ? entry.gateway : null
}

export type GatewayRequester = <T>(
  method: string,
  params?: Record<string, unknown>,
  timeoutMs?: number,
  signal?: AbortSignal
) => Promise<T>

export interface GatewayRequestLease {
  request: GatewayRequester
  release(): void
}

interface LeasedGatewayOwner {
  gateway: HermesGateway
  profile: string
  secondary: Secondary | null
}

function ownerStillRegistered(owner: LeasedGatewayOwner): boolean {
  return owner.secondary
    ? owner.secondary.commandLeases > 0 &&
        owner.secondary.wantOpen &&
        g.secondaries.get(owner.profile) === owner.secondary &&
        owner.secondary.gateway === owner.gateway
    : g.primaryGateway === owner.gateway && g.primaryProfile === owner.profile
}

async function reconnectPrimary(owner: LeasedGatewayOwner): Promise<void> {
  const current = g.primaryReconnect

  if (current?.gateway === owner.gateway && current.profile === owner.profile) {
    return current.promise
  }

  const attempt = (async () => {
    const desktop = window.hermesDesktop

    if (!desktop || !ownerStillRegistered(owner)) {
      return
    }

    const conn = await desktop.getConnection(owner.profile)
    const wsUrl = await resolveGatewayWsUrl(desktop, conn)

    if (!ownerStillRegistered(owner)) {
      return
    }

    await owner.gateway.connect(wsUrl)
  })()

  const reconnect = { gateway: owner.gateway, profile: owner.profile, promise: attempt }
  g.primaryReconnect = reconnect

  try {
    await attempt
  } finally {
    if (g.primaryReconnect === reconnect) {
      g.primaryReconnect = null
    }
  }
}

async function recoverLeasedGateway(owner: LeasedGatewayOwner): Promise<HermesGateway | null> {
  if (!ownerStillRegistered(owner)) {
    return null
  }

  if (isOpen(owner.gateway)) {
    return owner.gateway
  }

  if (owner.secondary) {
    clearTimer(owner.secondary)
    owner.secondary.reconnectAttempt = 0
    await reconnectSecondary(owner.secondary)
  } else {
    await reconnectPrimary(owner)
  }

  return ownerStillRegistered(owner) && isOpen(owner.gateway) ? owner.gateway : null
}

/**
 * Pin a concrete profile/socket for one async command. The lease is acquired
 * synchronously, so profile-switch pruning cannot dispose the source transport
 * while an earlier metadata lookup is pending. Requests retain the normal
 * disconnected → exact-owner reconnect → one retry semantics without ever
 * consulting the newly active gateway.
 */
export function acquireGatewayRequestLease(gateway: HermesGateway, profile: string): GatewayRequestLease {
  const key = normKey(profile)
  const secondary = g.secondaries.get(key) ?? null
  const owner: LeasedGatewayOwner = { gateway, profile: key, secondary }

  if (
    (secondary && secondary.gateway !== gateway) ||
    (!secondary && (g.primaryGateway !== gateway || g.primaryProfile !== key))
  ) {
    throw new Error('Hermes source gateway unavailable')
  }

  if (secondary) {
    secondary.commandLeases += 1
  }

  let released = false

  return {
    request: async <T>(method: string, params = {}, timeoutMs?: number, signal?: AbortSignal): Promise<T> => {
      if (released || !ownerStillRegistered(owner)) {
        throw new Error('Hermes source gateway unavailable')
      }

      try {
        return await gateway.request<T>(method, params, timeoutMs, signal)
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)

        if (!/not connected|connection closed/i.test(message)) {
          throw error
        }

        let recovered: HermesGateway | null

        try {
          recovered = await recoverLeasedGateway(owner)
        } catch (reconnectError) {
          if (isGatewayReauthRequired(reconnectError)) {
            throw reconnectError
          }

          throw error
        }

        if (!recovered || released || !ownerStillRegistered(owner)) {
          throw error
        }

        return recovered.request<T>(method, params, timeoutMs, signal)
      }
    },
    release: () => {
      if (released) {
        return
      }

      released = true

      if (secondary) {
        secondary.commandLeases = Math.max(0, secondary.commandLeases - 1)
      }
    }
  }
}

// Wake signal (sleep/network/visibility): nudge every live secondary back open.
export function reconnectSecondaryGateways(): void {
  for (const entry of g.secondaries.values()) {
    if (!entry.wantOpen || isOpen(entry.gateway)) {
      continue
    }

    entry.reconnectAttempt = 0
    clearTimer(entry)
    void reconnectSecondary(entry)
  }
}

// Keep the idle reaper from killing a backend we still need: ping every live
// secondary. The active one is pinged separately (touchActiveGatewayBackend).
export function touchSecondaryGateways(): void {
  const desktop = window.hermesDesktop

  for (const entry of g.secondaries.values()) {
    if (entry.wantOpen) {
      void desktop?.touchBackend?.(entry.profile).catch(() => undefined)
    }
  }
}

// Tear a secondary down: stop its reconnect loop, detach listeners, close the
// socket. Caller handles removal from the map.
function disposeSecondary(entry: Secondary): void {
  entry.wantOpen = false
  clearTimer(entry)
  entry.offEvent()
  entry.offState()
  entry.gateway.close()
}

// Close + evict secondaries whose profile is neither active nor in `keep`
// (profiles with a running / needs-input session). Bounds cost to live work.
export function pruneSecondaryGateways(keep: Set<string>): void {
  for (const [key, entry] of [...g.secondaries]) {
    if (key === g.activeKey || keep.has(key) || entry.commandLeases > 0) {
      continue
    }

    disposeSecondary(entry)
    g.secondaries.delete(key)
  }
}

export function closeSecondaryGateways(): void {
  for (const entry of g.secondaries.values()) {
    disposeSecondary(entry)
  }

  g.secondaries.clear()
}

// Self-accept so editing this module (or a fan-out that lands here) is an
// in-place hot update instead of a full page reload — the live sockets in `g`
// survive the swap. Dev-only: production strips import.meta.hot.
if (import.meta.hot) {
  import.meta.hot.accept()
}
