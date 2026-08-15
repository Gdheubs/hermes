import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'

import { setupMockBackend, waitForAppReady } from './fixtures'
import { expect, test } from './test'

test.setTimeout(240_000)

interface RemoteApiProbe {
  requests: string[]
  url: string
  close: () => Promise<void>
}

interface E2eDesktopBridge {
  api: <T>(request: { connectionId: string; path: string; profile: string }) => Promise<T>
  connections: {
    save: (payload: {
      allowPlainTextToken: boolean
      authMode: 'token'
      kind: 'remote'
      label: string
      token: string
      url: string
    }) => Promise<{ connection: { id: string } }>
  }
}

async function startRemoteApiProbe(): Promise<RemoteApiProbe> {
  const requests: string[] = []

  const server = createServer((request, response) => {
    const url = request.url ?? ''
    requests.push(url)
    response.setHeader('content-type', 'application/json')

    if (request.method === 'GET' && url === '/api/health') {
      response.end(JSON.stringify({ ok: true }))

      return
    }

    if (request.method === 'GET' && url === '/api/mcp/catalog?profile=worker') {
      response.end(JSON.stringify({ entries: [], source: 'remote-source' }))

      return
    }

    response.statusCode = 404
    response.end(JSON.stringify({ detail: `Unexpected request: ${request.method} ${url}` }))
  })

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => resolve())
  })

  const address = server.address() as AddressInfo

  return {
    requests,
    url: `http://127.0.0.1:${address.port}`,
    close: () => new Promise<void>((resolve, reject) => server.close(error => (error ? reject(error) : resolve())))
  }
}

test('routes an explicit Desktop API request to its registered source connection', async () => {
  const fixture = await setupMockBackend()
  const remote = await startRemoteApiProbe()

  try {
    // A cold Windows Python backend can take longer than the shared UI
    // fixture's interactive-ready default. This boundary test is concerned
    // with the post-boot IPC route, so it explicitly allows that cold start.
    await waitForAppReady(fixture, 180_000)

    const result = await fixture.page.evaluate(async remoteUrl => {
      const desktop = (window as unknown as { hermesDesktop: E2eDesktopBridge }).hermesDesktop

      const saved = await desktop.connections.save({
        allowPlainTextToken: true,
        authMode: 'token',
        kind: 'remote',
        label: 'E2E source route',
        token: 'e2e-source-token',
        url: remoteUrl
      })

      const response = await desktop.api<{ entries: unknown[]; source: string }>({
        connectionId: saved.connection.id,
        path: '/api/mcp/catalog',
        profile: 'worker'
      })

      return { connectionId: saved.connection.id, response }
    }, remote.url)

    expect(result.connectionId).toBeTruthy()
    expect(result.response).toEqual({ entries: [], source: 'remote-source' })
    expect(remote.requests).toContain('/api/health')
    expect(remote.requests).toContain('/api/mcp/catalog?profile=worker')
  } finally {
    await remote.close()
    await fixture.cleanup()
  }
})
