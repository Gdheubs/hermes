#!/usr/bin/env node
/**
 * Dry-run the INSTANT ACCOUNT first-launch experience end-to-end.
 *
 *   npm run dev:instant        (from apps/desktop; build first: npm run build)
 *
 * What it stages:
 *   1. A fake Nous Portal on a fixed local port (45741) — its PRESENCE is the
 *      feature flag the renderer probes. /instant/provision "mints" a guest
 *      account after a believable delay and hands back credentials pointing
 *      at an embedded mock inference server; /instant/claim links it.
 *   2. A mock OpenAI-compatible inference server (same shape as dev-mock.mjs)
 *      so the guest account can actually chat.
 *   3. A pristine sandbox HERMES_HOME with NO provider configured — the exact
 *      state that used to hit the onboarding wall.
 *
 * What you should see (the whole point):
 *   - The app opens straight into the chat. No provider picker, ever.
 *   - The composer is focused and usable from the first frame.
 *   - A quiet "Guest" chip sits in the statusbar; click it for the popover
 *     (what guest means, model, expiry, "Keep this setup").
 *   - Send a message immediately — even mid-mint it just works; the send
 *     waits on the mint invisibly.
 *   - After your SECOND completed turn, a "Keep this setup" pill appears in
 *     the suggestion strip. One click "claims" (fake) and everything guest
 *     quietly disappears.
 *
 * Knobs:
 *   INSTANT_MINT_DELAY_MS   fake token-exchange latency (default 1200)
 *   INSTANT_MINT_FAIL=1     token endpoint 500s — verifies the ladder falls
 *                           back to the classic onboarding overlay, unharmed.
 *   INSTANT_CREDITS_USD     starter grant shown in the chip (default 5)
 *   INSTANT_NO_LAUNCH=1     start the fake portal + mock inference only (no
 *                           Electron) — headless smoke testing of the mint
 *                           chain with curl.
 */

import http from 'node:http'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn, spawnSync } from 'node:child_process'

const DESKTOP_ROOT = path.resolve(import.meta.dirname, '..')
const REPO_ROOT = path.resolve(DESKTOP_ROOT, '..', '..')

// Must match INSTANT_PORTAL_URL in src/store/instant-account.ts.
const INSTANT_PORT = 45741

const MINT_DELAY_MS = Number.parseInt(process.env.INSTANT_MINT_DELAY_MS || '1200', 10)
const MINT_FAIL = process.env.INSTANT_MINT_FAIL === '1'
const INSTANT_CREDITS = Number.parseFloat(process.env.INSTANT_CREDITS_USD || '5')

const CANNED_REPLY =
  'Hey! Your guest account is live and this reply came through it — the whole instant chain is working.'

// ── Mock inference server (ephemeral port, same shape as dev-mock.mjs) ──

function startMockInference() {
  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      res.setHeader('Access-Control-Allow-Origin', '*')
      res.setHeader('Access-Control-Allow-Headers', '*')
      res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')

      if (req.method === 'OPTIONS') {
        res.writeHead(204)
        res.end()
        return
      }

      if (req.method === 'GET' && req.url === '/v1/models') {
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end(
          JSON.stringify({
            object: 'list',
            data: [{ id: 'hermes-guest', object: 'model', created: 0, owned_by: 'nous-guest' }],
          }),
        )
        return
      }

      if (req.method === 'POST' && req.url?.startsWith('/v1/chat/completions')) {
        let body = ''
        req.on('data', (chunk) => { body += chunk.toString() })
        req.on('end', () => {
          let parsed = {}
          try { parsed = JSON.parse(body) } catch { /* non-streaming */ }

          const stream = parsed.stream === true
          const model = parsed.model || 'hermes-guest'

          if (stream) {
            res.writeHead(200, {
              'Content-Type': 'text/event-stream',
              'Cache-Control': 'no-cache',
              Connection: 'keep-alive',
            })
            const words = CANNED_REPLY.split(' ')
            let i = 0
            const sendChunk = () => {
              if (i >= words.length) {
                res.write(
                  `data: ${JSON.stringify({
                    id: 'guest-completion', object: 'chat.completion.chunk',
                    created: 0, model,
                    choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
                  })}\n\n`,
                )
                res.write('data: [DONE]\n\n')
                res.end()
                return
              }
              const word = i === 0 ? words[i] : ' ' + words[i]
              res.write(
                `data: ${JSON.stringify({
                  id: 'guest-completion', object: 'chat.completion.chunk',
                  created: 0, model,
                  choices: [{ index: 0, delta: { content: word }, finish_reason: null }],
                })}\n\n`,
              )
              i++
              setTimeout(sendChunk, 24)
            }
            sendChunk()
          } else {
            res.writeHead(200, { 'Content-Type': 'application/json' })
            res.end(
              JSON.stringify({
                id: 'guest-completion', object: 'chat.completion',
                created: 0, model,
                choices: [{
                  index: 0,
                  message: { role: 'assistant', content: CANNED_REPLY },
                  finish_reason: 'stop',
                }],
                usage: { prompt_tokens: 10, completion_tokens: 24, total_tokens: 34 },
              }),
            )
          }
        })
        req.on('error', () => { res.writeHead(400); res.end('Bad request') })
        return
      }

      res.writeHead(404, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ error: 'Not found' }))
    })

    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address()
      if (addr === null || typeof addr === 'string') {
        reject(new Error('Failed to get inference server address'))
        return
      }
      resolve({ url: `http://127.0.0.1:${addr.port}`, close: () => server.close() })
    })
  })
}

// ── Fake Nous Portal ────────────────────────────────────────────────────
//
// Speaks nous-account-service#894's device-attestation grant so the client
// exercises the REAL wire shape: nonce → form-encoded token exchange →
// Bearer-authed /api/oauth/account (payload mirroring
// src/app/api/oauth/account/route.ts). The attestation JWS is accepted
// without signature verification — chain validation is the real portal's
// job; this fake validates the flow, not the crypto.

function startFakePortal(inferenceUrl) {
  const nonces = new Set()
  let minted = null
  let claimed = false

  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      res.setHeader('Access-Control-Allow-Origin', '*')
      res.setHeader('Access-Control-Allow-Headers', '*')
      res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')

      if (req.method === 'OPTIONS') {
        res.writeHead(204)
        res.end()
        return
      }

      const json = (code, payload) => {
        res.writeHead(code, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify(payload))
      }

      if (req.method === 'POST' && req.url === '/api/oauth/device-attestation/nonce') {
        const nonce = `nonce-${Math.random().toString(36).slice(2)}`
        nonces.add(nonce)
        json(200, { nonce })
        return
      }

      if (req.method === 'POST' && req.url === '/api/oauth/token') {
        let body = ''
        req.on('data', (chunk) => { body += chunk.toString() })
        req.on('end', () => {
          setTimeout(() => {
            const params = new URLSearchParams(body)

            if (params.get('grant_type') !== 'urn:nous:grant-type:device-attestation') {
              json(400, { error: 'unsupported_grant_type' })
              return
            }
            if (params.get('client_id') !== 'hermes-laptop') {
              json(400, { error: 'invalid_client' })
              return
            }

            // Decode the JWS payload for device_id + nonce; skip signature
            // verification (the real portal chains to the OEM root — that
            // half is server crypto, not client contract).
            let payload = {}
            try {
              const segment = params.get('device_attestation')?.split('.')[1] ?? ''
              payload = JSON.parse(Buffer.from(segment, 'base64url').toString('utf8'))
            } catch { /* structurally invalid */ }

            if (!payload.nonce || !nonces.delete(payload.nonce)) {
              json(400, { error: 'invalid_grant', error_description: 'nonce unknown or already consumed' })
              return
            }

            if (MINT_FAIL) {
              console.log('[portal] token → 500 (INSTANT_MINT_FAIL=1)')
              json(500, { error: 'provisioning disabled' })
              return
            }

            const device = payload.device_id || `device-${Math.random().toString(36).slice(2, 8)}`
            // Idempotent provision-or-fetch: same device re-attests into the
            // same shadow account, no second grant (mirrors #894).
            if (!minted || minted.device !== device) {
              minted = {
                device,
                org: { id: `org-${device}`, slug: `guest-${device.slice(-6)}`, name: 'Personal' },
                access_token: `guest-token-${Math.random().toString(36).slice(2)}`,
                credits: INSTANT_CREDITS,
              }
              console.log(`[portal] minted shadow account ${minted.org.slug} ($${minted.credits}) (${MINT_DELAY_MS}ms)`)
            } else {
              console.log(`[portal] re-attested ${minted.org.slug} — no second grant`)
            }

            json(200, {
              access_token: minted.access_token,
              refresh_token: `guest-refresh-${Math.random().toString(36).slice(2)}`,
              token_type: 'Bearer',
              expires_in: 3600,
              // Dev-fake extension: the renderer needs somewhere to point the
              // credential; in production this is client-side auth state, not
              // a token-response field.
              inference_base_url: `${inferenceUrl}/v1`,
              model: 'hermes-guest',
            })
          }, MINT_DELAY_MS)
        })
        return
      }

      if (req.method === 'GET' && req.url === '/api/oauth/account') {
        if (!minted || req.headers.authorization !== `Bearer ${minted.access_token}`) {
          json(401, { error: 'invalid_token' })
          return
        }
        // Shape of src/app/api/oauth/account/route.ts for a shadow account:
        // no email, no Privy identity, no subscription — just the personal
        // org and the granted credit.
        json(200, {
          user: { email: null, privy_did: null },
          organisation: minted.org,
          subscription: null,
          purchased_credits_remaining: 0,
          tool_access: { tool_pool_credits_usd: 0, tool_pool_gated_off: false },
          paid_service_access: {
            allowed: true,
            paid_access: true,
            reason: 'device_grant',
            organisation_id: minted.org.id,
            has_active_subscription: false,
            active_subscription_is_paid: false,
            subscription_tier: null,
            subscription_credits_remaining: 0,
            purchased_credits_remaining: 0,
            total_usable_credits: minted.credits,
          },
        })
        return
      }

      if (req.method === 'POST' && req.url === '/instant/claim') {
        // #894 defers claim/merge server-side; this stub is the client's
        // stand-in until that follow-up exists.
        setTimeout(() => {
          claimed = true
          console.log(`[portal] claimed ${minted?.org.slug ?? '(unknown org)'}`)
          json(200, { ok: true, org: minted?.org.slug ?? null, claimed })
        }, 900)
        return
      }

      json(404, { error: 'Not found' })
    })

    server.on('error', reject)
    server.listen(INSTANT_PORT, '127.0.0.1', () => {
      resolve({ url: `http://127.0.0.1:${INSTANT_PORT}`, close: () => server.close() })
    })
  })
}

// ── Sandbox: a genuinely unconfigured install ───────────────────────────

function createSandbox() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `hermes-instant-${Date.now()}`))
  const hermesHome = path.join(root, 'hermes-home')
  const userDataDir = path.join(root, 'electron-user-data')
  fs.mkdirSync(hermesHome, { recursive: true })
  fs.mkdirSync(userDataDir, { recursive: true })
  // Deliberately NO config.yaml and NO .env: this is the fresh-download state
  // that used to be greeted by the provider wall.
  return { root, hermesHome, userDataDir, cleanup: () => fs.rmSync(root, { recursive: true, force: true }) }
}

// ── Electron launch ─────────────────────────────────────────────────────

function findElectron() {
  const local = path.join(REPO_ROOT, 'node_modules', 'electron', 'dist', 'electron')
  if (fs.existsSync(local)) return local
  const r = spawnSync('which', ['electron'], { encoding: 'utf8' })
  if (r.status === 0 && r.stdout.trim()) return r.stdout.trim()
  throw new Error('Electron binary not found. Run "npm install" from the repo root.')
}

function assertDistBuilt() {
  const electronMain = path.join(DESKTOP_ROOT, 'dist', 'electron-main.mjs')
  const indexHtml = path.join(DESKTOP_ROOT, 'dist', 'index.html')
  if (!fs.existsSync(electronMain) || !fs.existsSync(indexHtml)) {
    throw new Error(
      `Desktop dist not built. Run 'cd apps/desktop && npm run build' first.\n` +
      `Missing: ${electronMain}`,
    )
  }
}

// ── Main ────────────────────────────────────────────────────────────────

async function main() {
  const serversOnly = process.env.INSTANT_NO_LAUNCH === '1'

  if (!serversOnly) {
    assertDistBuilt()
  }

  console.log('Instant-account dry run')
  console.log(`  mint delay: ${MINT_DELAY_MS}ms${MINT_FAIL ? '  (FAIL MODE — expect the classic overlay)' : ''}`)

  const inference = await startMockInference()
  console.log(`  mock inference: ${inference.url}`)

  const portal = await startFakePortal(inference.url)
  console.log(`  fake portal:    ${portal.url}`)

  if (serversOnly) {
    console.log('')
    console.log('  INSTANT_NO_LAUNCH=1 — servers up, no Electron. Ctrl-C to stop.')
    console.log(`  Try: curl -X POST ${portal.url}/api/oauth/device-attestation/nonce`)
    return
  }

  const sandbox = createSandbox()
  console.log(`  HERMES_HOME:    ${sandbox.hermesHome}  (unconfigured — no keys, no config)`)

  const electronBin = findElectron()

  const env = {
    ...process.env,
    HERMES_HOME: sandbox.hermesHome,
    HERMES_DESKTOP_USER_DATA_DIR: sandbox.userDataDir,
    HERMES_DESKTOP_IGNORE_EXISTING: '1',
    HERMES_DESKTOP_HERMES_ROOT: REPO_ROOT,
    HERMES_DESKTOP_APP_NAME: `HermesInstant-${Date.now()}`,
  }

  console.log('Launching Electron…')
  console.log('')
  console.log('  Watch for: no provider wall → focused composer → "Guest" chip')
  console.log('  in the statusbar → send twice → "Keep this setup" pill.')
  console.log('')
  const child = spawn(electronBin, [DESKTOP_ROOT, '--disable-gpu', '--no-sandbox'], {
    env,
    cwd: DESKTOP_ROOT,
    stdio: 'inherit',
  })

  child.on('exit', (code) => {
    portal.close()
    inference.close()
    sandbox.cleanup()
    process.exit(code ?? 0)
  })
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
