/**
 * The resolved Desktop connection mode — the one connection fact extensions are
 * allowed to see (NousResearch/hermes-agent#82140).
 *
 * `local`  — this Desktop drives its own local backend, so a path the agent
 *            reports is already a path on the machine the user is looking at.
 * `remote` — this Desktop drives an SSH/URL/cloud backend, so a gateway-side
 *            path has to be transferred before the Desktop can open it.
 *
 * That distinction is the whole point: without it an extension can't tell
 * whether `/home/user/report.md` is openable here, which is what makes `MEDIA:`
 * and file-link handling ambiguous on remote gateways.
 *
 * Everything else on the connection descriptor — base URL, host, identity,
 * tokens, auth mode — stays behind the Electron bridge. Extensions get the
 * shape of the connection, never the credentials for it.
 */

import type { HermesConnection } from '@/global'
import { $connection } from '@/store/session'

export type HermesConnectionMode = 'local' | 'remote'

/** RPCs on which the renderer announces its live mode to the backend. Session
 *  lifecycle pins it for new/resumed chats; `prompt.submit` re-announces every
 *  turn so switching the active connection or profile lands immediately rather
 *  than being stuck at whatever was true when the chat opened. */
const CONNECTION_MODE_METHODS = new Set(['prompt.submit', 'session.create', 'session.resume'])

/**
 * Narrow a connection descriptor to its mode.
 *
 * The descriptor's `mode` is already resolved (a `cloud` saved config resolves
 * to a `remote` connection), so this only has to guard the "no descriptor yet"
 * and "older shell that predates the field" cases — both of which resolve to
 * null. Null means "unknown", never "local": telling an extension a remote file
 * is local hands the user a link to a file that isn't on their machine.
 */
export function resolveConnectionMode(connection: HermesConnection | null | undefined): HermesConnectionMode | null {
  const mode = connection?.mode

  return mode === 'local' || mode === 'remote' ? mode : null
}

/**
 * Stamp `connection_mode` onto the params of an RPC that carries it.
 *
 * Applied at the single `requestGateway` choke point rather than at each of the
 * ~10 call sites, so a new session/prompt path announces correctly by
 * construction instead of by remembering to.
 *
 * `connection_mode` is a RESERVED, renderer-owned field: whatever the caller
 * put there is discarded and the live resolved mode is written in its place.
 * The plugin SDK's `host.request` reaches this same door, so honouring a
 * caller value would let a plugin driving a live REMOTE session announce
 * `local` and have the backend hand its skills and MCP context paths as though
 * they were on the user's machine — the exact spoof the field exists to
 * prevent. Only the renderer can see the descriptor, so only the renderer may
 * answer.
 *
 * An unresolved mode announces an explicit `null` rather than omitting the
 * key. Omitting it means "leave the stored value alone" to the backend
 * (`_remember_connection_mode`), so a `local` announced before a reconnect
 * would survive into turns that can no longer prove it — and "unknown must
 * never be guessed as local" is the whole safety rule here.
 * `normalize_desktop_connection_mode(None)` is `None`, so this clears it.
 */
export function withConnectionMode(
  method: string,
  params: Record<string, unknown>,
  mode: HermesConnectionMode | null
): Record<string, unknown> {
  if (!CONNECTION_MODE_METHODS.has(method)) {
    return params
  }

  return { ...params, connection_mode: mode }
}

/**
 * The one announcement helper every gateway-request door shares.
 *
 * Reads the live `$connection` at CALL time (so a retry announces the mode of
 * the connection it actually lands on) and stamps it via `withConnectionMode`.
 * Both `useGatewayRequest` (app/hook callers) and the plugin SDK's
 * `host.request` go through here — a request door that skips it lets a plugin
 * drive a Desktop session whose skills/MCP context never learns the mode.
 */
export function announceConnectionMode(method: string, params: Record<string, unknown>): Record<string, unknown> {
  return withConnectionMode(method, params, resolveConnectionMode($connection.get()))
}
