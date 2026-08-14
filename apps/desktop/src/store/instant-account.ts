import { atom } from 'nanostores'

import { translateNow } from '@/i18n'
import { setMainModelAssignment } from '@/store/cron-model-impact'
import { $gateway } from '@/store/gateway'
import { notify, notifyError } from '@/store/notifications'
import { refreshOnboarding, requestDesktopOnboarding } from '@/store/onboarding'

/**
 * Instant account — the "open the app → start chatting" path.
 *
 * On a fresh, unconfigured install the desktop silently provisions a guest
 * account instead of walling the user behind the provider picker: the
 * composer is usable from the first frame, the mint races the user's first
 * keystroke in the background (a send waits on it invisibly — nobody can
 * tell 600ms of mint from 600ms of model latency), and the account is
 * *claimable* later rather than demanded up front.
 *
 * WIRE SHAPE: this speaks nous-account-service's device-attestation grant
 * (NousResearch/nous-account-service#894) so the production swap is a URL
 * change, not a protocol change:
 *
 *   1. POST /api/oauth/device-attestation/nonce        → { nonce }
 *   2. POST /api/oauth/token                            (form-encoded)
 *        grant_type=urn:nous:grant-type:device-attestation
 *        client_id=hermes-laptop, device_attestation=<JWS>
 *      → { access_token, … } — a shadow account: normal User + personal
 *        org, no email, `account_tier: "device"` claims, one-time credit
 *        grant. The access token IS the inference credential.
 *   3. GET /api/oauth/account                           (Bearer)
 *      → the standard portal account payload; guests read credits from
 *        paid_service_access.total_usable_credits and are recognizable by
 *        user.email === null.
 *
 * The attestation JWS itself is the faked part in the dry run — real signing
 * is hardware-side (see hermes-agent's setup-rtx mock TPM), not this store's
 * job. The nonce probe doubles as the feature flag: portal absent/refusing →
 * 'off', and the classic onboarding overlay renders untouched.
 *
 * Ladder honesty: no portal → 'off'; failed mint → 'failed'; both fall to
 * the classic overlay. This store only ever *removes* the wall when it can
 * actually deliver a working provider.
 */

// Fixed local port shared with scripts/dev-instant.mjs (INSTANT_PORT there).
export const INSTANT_PORTAL_URL = 'http://127.0.0.1:45741'

const DEVICE_GRANT_TYPE = 'urn:nous:grant-type:device-attestation'
const DEVICE_CLIENT_ID = 'hermes-laptop'

const PROBE_TIMEOUT_MS = 500
const MINT_RETRY_MS = 750
const MINT_RETRY_MAX = 60
const CONFIRM_RETRY_MS = 1_500
const CONFIRM_RETRY_MAX = 3

// Mirrors onboarding.ts's CONFIGURED_CACHE_KEY — read-only here, to skip the
// probe entirely on installs that are already configured.
const CONFIGURED_CACHE_KEY = 'hermes-desktop-onboarded-v1'
const GUEST_CACHE_KEY = 'hermes-instant-guest-v1'

export type InstantAccountStatus =
  | 'claimed'
  | 'claiming'
  | 'failed'
  | 'idle'
  | 'minting'
  | 'off'
  | 'ready'

export interface InstantAccountState {
  status: InstantAccountStatus
  /** Personal-org slug of the shadow account (e.g. "guest-x7k2m9"). */
  org: null | string
  /** Usable credit in USD (paid_service_access.total_usable_credits) —
   *  surfaced in the chip popover from day one so the budget is never a
   *  surprise. Null until the account payload lands. */
  creditsUsd: null | number
  model: null | string
}

const INITIAL: InstantAccountState = { status: 'idle', org: null, creditsUsd: null, model: null }

export const $instantAccount = atom<InstantAccountState>(INITIAL)

const patch = (update: Partial<InstantAccountState>) => $instantAccount.set({ ...$instantAccount.get(), ...update })

interface GuestCacheRecord {
  claimed?: boolean
  creditsUsd?: number
  model?: string
  org?: string
}

function readGuestCache(): GuestCacheRecord | null {
  try {
    const raw = window.localStorage.getItem(GUEST_CACHE_KEY)

    return raw ? (JSON.parse(raw) as GuestCacheRecord) : null
  } catch {
    return null
  }
}

function writeGuestCache(record: GuestCacheRecord | null) {
  try {
    if (record) {
      window.localStorage.setItem(GUEST_CACHE_KEY, JSON.stringify(record))
    } else {
      window.localStorage.removeItem(GUEST_CACHE_KEY)
    }
  } catch {
    // localStorage unavailable — degrade silently.
  }
}

const sleep = (ms: number) => new Promise<void>(resolve => window.setTimeout(resolve, ms))

async function portalFetch(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`${INSTANT_PORTAL_URL}${path}`, {
    ...init,
    signal: init?.signal ?? AbortSignal.timeout(15_000)
  })

  if (!response.ok) {
    throw new Error(`Instant portal ${path} responded ${response.status}`)
  }

  return response
}

const portalJson = async <T,>(path: string, init?: RequestInit): Promise<T> =>
  (await (await portalFetch(path, init)).json()) as T

/** True while the chip should sit in the statusbar: the deal has a small,
 *  true label on the corner until the account is claimed. */
export function isGuestChipVisible(status: InstantAccountStatus): boolean {
  return status === 'ready' || status === 'claiming'
}

/** Statuses during which the onboarding overlay must stay out of the way —
 *  the instant path owns first-run, and un-suppressing mid-mint would flash
 *  the exact wall this feature deletes. 'idle' suppresses too: it lasts one
 *  local probe (≤500ms, behind the boot overlay) and resolving it to 'off'
 *  falls through to the classic overlay unharmed. */
export function instantSuppressesOnboarding(status: InstantAccountStatus): boolean {
  return status !== 'off' && status !== 'failed'
}

/**
 * Resolves the moment sends may proceed. While the mint is in flight the
 * first Enter waits here instead of failing against a provider that is one
 * breath away from existing — the race between the mint and the user's first
 * message has no visible loser. Every terminal state resolves immediately;
 * the mint's own bounded retries/timeouts guarantee this settles.
 */
export function instantAccountSettled(): Promise<void> {
  if ($instantAccount.get().status !== 'minting') {
    return Promise.resolve()
  }

  return new Promise(resolve => {
    const unsubscribe = $instantAccount.subscribe(state => {
      if (state.status !== 'minting') {
        unsubscribe()
        resolve()
      }
    })
  })
}

const b64url = (value: string) => window.btoa(value).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')

/** Dev-run attestation: the JWS *shape* (header.payload.signature binding
 *  device_id + nonce + audience) without a real signature — signing is the
 *  TPM's job on real hardware and the fake portal doesn't verify. The wire
 *  contract this satisfies is #894's `device_attestation` token parameter. */
function mockAttestationJws(deviceId: string, nonce: string): string {
  const header = b64url(JSON.stringify({ alg: 'ES256', typ: 'JWT', x5c: ['mock-leaf-cert'] }))

  const payload = b64url(
    JSON.stringify({ aud: 'nous-device-attestation', device_id: deviceId, exp: Math.floor(Date.now() / 1000) + 300, nonce })
  )

  return `${header}.${payload}.${b64url('mock-signature')}`
}

function deviceId(): string {
  const cached = readGuestCache()

  if (cached?.org) {
    // Re-attesting resolves to the SAME shadow account (grant is idempotent
    // server-side), so a stable device id per install is the correct key.
    return cached.org
  }

  return `desktop-${Math.random().toString(36).slice(2, 10)}`
}

interface TokenResponse {
  access_token: string
  expires_in: number
  refresh_token: string
  token_type: string
  /** Dev-fake extension: where the minted credential points. In production
   *  this is client-side state from the auth flow (hermes_cli stores
   *  inference_base_url next to the token), not a token-response field. */
  inference_base_url: string
  model: string
}

interface AccountResponse {
  organisation: { id: string; name: string; slug: string }
  paid_service_access: { total_usable_credits: number }
  user: { email: null | string; privy_did: null | string }
}

async function mint(): Promise<void> {
  patch({ status: 'minting' })

  try {
    // Step 1 already ran as the boot probe; the nonce is handed in.
    const nonce = pendingNonce

    if (!nonce) {
      throw new Error('no attestation nonce')
    }

    // Step 2: exchange the (mock) attestation for a shadow-account token.
    const device = deviceId()

    const body = new URLSearchParams({
      client_id: DEVICE_CLIENT_ID,
      device_attestation: mockAttestationJws(device, nonce),
      grant_type: DEVICE_GRANT_TYPE
    })

    const grant = await portalJson<TokenResponse>('/api/oauth/token', {
      body,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      method: 'POST'
    })

    // The backend may still be finishing boot — the mint quietly outwaits it.
    // Bounded: ~45s of retries, then the ladder falls to the classic overlay.
    let lastError: unknown = null

    for (let attempt = 0; attempt < MINT_RETRY_MAX; attempt++) {
      try {
        await setMainModelAssignment({
          provider: 'custom',
          model: grant.model,
          base_url: grant.inference_base_url,
          api_key: grant.access_token
        })
        lastError = null

        break
      } catch (error) {
        lastError = error
        await sleep(MINT_RETRY_MS)
      }
    }

    if (lastError !== null) {
      throw lastError
    }

    await $gateway.get()?.request('reload.env').catch(() => undefined)

    // Step 3: the standard account payload — org identity + credit budget.
    const account = await portalJson<AccountResponse>('/api/oauth/account', {
      headers: { Authorization: `Bearer ${grant.access_token}` }
    })

    const credits = account.paid_service_access.total_usable_credits

    writeGuestCache({ org: account.organisation.slug, creditsUsd: credits, model: grant.model })
    patch({ status: 'ready', org: account.organisation.slug, creditsUsd: credits, model: grant.model })

    // Hand completion to the onboarding store's own machinery: refresh sees a
    // ready runtime, marks the app configured, and resets `requested` — which
    // re-fires the overlay's own effect with the REAL wiring ctx, so
    // onCompleted (config + model + options refresh) runs exactly like a
    // hand-configured provider. Bounded re-checks cover a backend that needs
    // a beat to see the new assignment.
    const ctx = {
      requestGateway: <T,>(method: string, params?: Record<string, unknown>) => {
        const gateway = $gateway.get()

        if (!gateway) {
          return Promise.reject(new Error('gateway not connected'))
        }

        return gateway.request(method, params) as Promise<T>
      }
    }

    for (let attempt = 0; attempt < CONFIRM_RETRY_MAX; attempt++) {
      if (await refreshOnboarding(ctx).catch(() => false)) {
        // Configured. Nudge the overlay's own effect — it holds the REAL
        // wiring ctx whose onCompleted refreshes config + model + options.
        // Invisible: the overlay render is suppressed while we're 'ready'.
        requestDesktopOnboarding()

        return
      }

      await sleep(CONFIRM_RETRY_MS)
    }
  } catch {
    // Fail soft and fall down the ladder: the classic onboarding overlay
    // renders as if this feature never existed. No error chrome — an
    // unprovisioned first run is a normal state, not a failure screen.
    patch({ status: 'failed' })
  }
}

/** Link the shadow account to a real identity, keeping the org (and with it
 *  the user's history) — #894 defers this server-side ("no claim/merge flow
 *  in this PR"), so the dev portal stubs it; the button and copy are the
 *  client half waiting on that follow-up. */
export async function claimInstantAccount(): Promise<void> {
  const current = $instantAccount.get()

  if (current.status !== 'ready') {
    return
  }

  patch({ status: 'claiming' })

  try {
    await portalJson<{ ok: boolean }>('/instant/claim', { method: 'POST' })

    writeGuestCache({ ...(readGuestCache() ?? {}), claimed: true })
    patch({ status: 'claimed' })
    notify({
      kind: 'success',
      title: translateNow('shell.guestAccount.claimedTitle'),
      message: translateNow('shell.guestAccount.claimedMessage')
    })
  } catch (error) {
    patch({ status: 'ready' })
    notifyError(error, translateNow('shell.guestAccount.claimFailed'))
    throw error
  }
}

let pendingNonce: null | string = null

async function boot(): Promise<void> {
  // Desktop-only: the web dashboard and test environments have no bridge and
  // no first-run wall to remove.
  if (typeof window === 'undefined' || !window.hermesDesktop) {
    patch({ status: 'off' })

    return
  }

  const cached = readGuestCache()

  // Already configured: either this install minted a guest before (restore
  // the chip so the claim path survives relaunch) or the user configured a
  // provider themselves (stay out of the way entirely).
  if (window.localStorage.getItem(CONFIGURED_CACHE_KEY) === '1') {
    if (cached?.org && !cached.claimed) {
      patch({
        status: 'ready',
        org: cached.org,
        creditsUsd: cached.creditsUsd ?? null,
        model: cached.model ?? null
      })
    } else {
      patch({ status: cached?.claimed ? 'claimed' : 'off' })
    }

    return
  }

  // Unconfigured install: the nonce endpoint is both the first real step of
  // the grant AND the feature probe — reachable and willing means the
  // program exists; dead localhost settles in milliseconds and means it
  // doesn't. No bespoke health route to diverge from the real portal.
  try {
    const { nonce } = await portalJson<{ nonce: string }>('/api/oauth/device-attestation/nonce', {
      method: 'POST',
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS)
    })

    pendingNonce = nonce
  } catch {
    patch({ status: 'off' })

    return
  }

  await mint()
}

void boot()
