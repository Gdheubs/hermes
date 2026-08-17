// Helpers for saving a gateway-hosted file to the local disk from the Electron
// main process. Extracted from main.ts so the streaming, data-URL decoding, and
// filename derivation are unit-testable without spinning up Electron.
//
// The transport wrappers (token / OAuth) live in main.ts because they need
// main-process singletons (https/http, electronNet, the OAuth session). They
// delegate the byte-moving to `pumpStreamToFile` here, which streams the
// response to a user-selected destination with backpressure and cleans up a
// partial file on error — so a large download never has to be buffered whole in
// the native process.

import path from 'node:path'

// Minimal shape of the response objects we consume. Both Node's
// http.IncomingMessage and Electron net's IncomingMessage satisfy it.
export interface ReadableLike {
  on(event: 'data', listener: (chunk: Buffer | Uint8Array | string) => void): unknown
  on(event: 'end', listener: () => void): unknown
  on(event: 'error', listener: (err: Error) => void): unknown
  pause?: () => void
  resume?: () => void
  destroy?: (err?: Error) => void
}

export interface WriteStreamLike {
  write(chunk: Buffer): boolean
  end(cb: () => void): void
  destroy(err?: Error): void
  on(event: 'error', listener: (err: Error) => void): unknown
  once(event: 'drain', listener: () => void): unknown
}

// Upper bound for a gateway download body. The managed /api/files routes cap
// server-side, but saveGatewayFile also talks to the broad /api/fs/download
// route, and a client must not trust a remote peer to stay bounded: enforce
// the limit locally, both on the announced Content-Length (before the save
// dialog opens) and on the actual streamed byte count.
export const GATEWAY_DOWNLOAD_MAX_BYTES = 5 * 1024 ** 3

// Thrown when a gateway response announces or streams more than
// GATEWAY_DOWNLOAD_MAX_BYTES bytes. Distinct type so callers can tell an
// over-limit rejection apart from transport/IO failures.
export class GatewayDownloadTooLargeError extends Error {
  constructor() {
    super(`Gateway download exceeds the ${GATEWAY_DOWNLOAD_MAX_BYTES}-byte client limit`)
    this.name = 'GatewayDownloadTooLargeError'
  }
}

// Pre-flight check against the response headers, run before the save dialog
// opens so an over-limit download fails fast instead of after the user picks a
// destination. Absent/invalid Content-Length is NOT a pass to skip the stream
// count — pumpStreamToFile still enforces the bound while bytes flow.
export function contentLengthExceedsLimit(headers: Record<string, unknown>, maxBytes = GATEWAY_DOWNLOAD_MAX_BYTES): boolean {
  const raw = headers['content-length'] ?? headers['Content-Length']
  const value = Array.isArray(raw) ? raw[0] : raw
  const length = Number(value)

  return Number.isFinite(length) && length > maxBytes
}

export interface PumpDeps {
  createWriteStream: (destPath: string) => WriteStreamLike
  unlink: (destPath: string) => Promise<unknown>
}

// Stream `res` into `destPath`, honoring backpressure. On any read/write error
// the write stream is torn down and the (partial) destination file is removed
// before the returned promise rejects, so a failed download never leaves a
// truncated file behind. When maxBytes is given, the stream fails with
// GatewayDownloadTooLargeError once more than maxBytes bytes arrive.
export function pumpStreamToFile(res: ReadableLike, destPath: string, deps: PumpDeps, maxBytes?: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const ws = deps.createWriteStream(destPath)
    let failed = false
    let received = 0

    const fail = (err: Error) => {
      if (failed) {
        return
      }

      failed = true

      try {
        res.destroy?.(err)
      } catch {
        // best effort — the socket may already be closed
      }

      try {
        ws.destroy()
      } catch {
        // best effort
      }

      Promise.resolve(deps.unlink(destPath))
        .catch(() => {})
        .then(() => reject(err))
    }

    ws.on('error', fail)
    res.on('error', fail)

    res.on('data', chunk => {
      if (failed) {
        return
      }

      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as Uint8Array)

      received += buffer.length

      if (maxBytes !== undefined && received > maxBytes) {
        fail(new GatewayDownloadTooLargeError())

        return
      }

      const ok = ws.write(buffer)

      // Backpressure: pause the source until the file stream drains so we never
      // accumulate the whole payload in memory.
      if (!ok && typeof res.pause === 'function') {
        res.pause()
        ws.once('drain', () => {
          if (!failed) {
            res.resume?.()
          }
        })
      }
    })

    res.on('end', () => {
      if (failed) {
        return
      }

      ws.end(() => resolve())
    })
  })
}

export interface AtomicPumpDeps extends PumpDeps {
  mkdtemp: (prefix: string) => Promise<string>
  rename: (oldPath: string, newPath: string) => Promise<unknown>
  rm: (target: string, options: { force: boolean; recursive: boolean }) => Promise<unknown>
}

// Atomic variant of pumpStreamToFile: the body lands in a temp file inside the
// destination directory and is renamed onto `destPath` only after the stream
// completed in full. A failed download therefore leaves a pre-existing
// destination byte-identical — writing directly into the final pathname and
// unlinking it by name on error both corrupts the original mid-transfer and
// races a concurrent replacement at the same path.
//
// POSIX rename(2) replaces an existing destination atomically. Windows refuses
// to rename over an existing file, so there the destination is unlinked first
// and then renamed into place: still never a partial file at the final path,
// only a small no-file window after the user explicitly confirmed replacement.
export async function pumpStreamToFileAtomically(
  res: ReadableLike,
  destPath: string,
  deps: AtomicPumpDeps,
  maxBytes?: number
): Promise<void> {
  const tempDir = await deps.mkdtemp(path.join(path.dirname(destPath), '.hermes-download-'))
  const tempPath = path.join(tempDir, 'payload')

  try {
    await pumpStreamToFile(res, tempPath, deps, maxBytes)

    try {
      await deps.rename(tempPath, destPath)
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code

      if (code !== 'EEXIST' && code !== 'EPERM' && code !== 'ENOTEMPTY') {
        throw error
      }

      // Windows fallback documented above: replace after confirmed success.
      await deps.unlink(destPath)
      await deps.rename(tempPath, destPath)
    }
  } finally {
    await deps.rm(tempDir, { force: true, recursive: true }).catch(() => undefined)
  }
}

// Decode a `data:[<mime>][;base64],<payload>` URL into a Buffer. Used by the
// compatibility fallback that reads through the capped `/api/fs/read-data-url`
// route when the gateway predates `/api/fs/download`.
export function parseDataUrlToBuffer(dataUrl: string): Buffer {
  const match = /^data:([^,]*),([\s\S]*)$/.exec(String(dataUrl || ''))

  if (!match) {
    throw new Error('Malformed data URL')
  }

  const meta = match[1] || ''
  const payload = match[2] || ''

  if (/;base64/i.test(meta)) {
    return Buffer.from(payload, 'base64')
  }

  return Buffer.from(decodeURIComponent(payload), 'utf8')
}

// Extract a filename from a Content-Disposition header, preferring the RFC 5987
// `filename*` form. Returns '' when none is present. Always reduced to a
// basename so a malicious header can't redirect the save outside the picked dir.
export function filenameFromContentDisposition(value: unknown): string {
  const text = String(value || '')
  const encoded = text.match(/filename\*=(?:UTF-8'')?([^;]+)/i)?.[1]
  const plain = text.match(/filename="?([^";]+)"?/i)?.[1]
  const raw = encoded || plain || ''

  if (!raw) {
    return ''
  }

  try {
    return path.basename(decodeURIComponent(raw.trim()))
  } catch {
    return path.basename(raw.trim())
  }
}

// Normalize a gateway file path that may arrive as a bare path or a file:// URL.
export function gatewayFilePath(rawPath: unknown): string {
  const value = String(rawPath || '').trim()

  if (!value) {
    return ''
  }

  if (!/^file:/i.test(value)) {
    return value
  }

  try {
    return decodeURIComponent(new URL(value).pathname)
  } catch {
    return value.replace(/^file:\/\//i, '')
  }
}

// True when an error thrown by a transport wrapper represents an HTTP 404, used
// to trigger the data-URL compatibility fallback (and nothing else).
export function isNotFoundError(error: unknown): boolean {
  return Boolean(error) && (error as { statusCode?: number }).statusCode === 404
}
