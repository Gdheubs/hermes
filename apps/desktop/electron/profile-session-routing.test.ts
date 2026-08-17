import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  buildSidebarSessionSliceParams,
  fetchPrimaryProfileSessions,
  fetchRemoteProfileSessions,
  mergeProfileSessionWindow,
  remoteProfileSharesGateway
} from './profile-session-routing'

test('remote sidebar slices all follow the selected profile', () => {
  const slices = buildSidebarSessionSliceParams(
    new URLSearchParams({
      recents_profile: 'work-vps',
      recents_limit: '30',
      cron_limit: '40',
      messaging_limit: '50',
      recents_exclude: 'cron,signal',
      messaging_exclude: 'desktop,cron'
    })
  )

  assert.equal(slices.recents.get('profile'), 'work-vps')
  assert.equal(slices.cron.get('profile'), 'work-vps')
  assert.equal(slices.messaging.get('profile'), 'work-vps')
  assert.equal(slices.recents.get('exclude_sources'), 'cron,signal')
  assert.equal(slices.cron.get('source'), 'cron')
  assert.equal(slices.messaging.get('exclude_sources'), 'desktop,cron')
})

test('remote sidebar slices preserve the explicit all-profiles scope', () => {
  const slices = buildSidebarSessionSliceParams(new URLSearchParams({ recents_profile: 'all' }))

  assert.deepEqual(
    Object.values(slices).map(params => params.get('profile')),
    ['all', 'all', 'all']
  )
})

test('remote sidebar slices fall back to the all-profiles scope and default limits', () => {
  for (const searchParams of [new URLSearchParams(), new URLSearchParams({ recents_profile: '   ' })]) {
    const slices = buildSidebarSessionSliceParams(searchParams)

    assert.deepEqual(
      Object.values(slices).map(params => params.get('profile')),
      ['all', 'all', 'all']
    )
    assert.equal(slices.recents.get('limit'), '20')
    assert.equal(slices.cron.get('limit'), '50')
    assert.equal(slices.messaging.get('limit'), '100')
  }
})

test('primary session reads use the profile-aware request path', async () => {
  const calls: Array<{ profile: string | null; path: string }> = []
  const expected = { sessions: [{ id: 'session-1' }], total: 1, profile_totals: { default: 1 } }

  const result = await fetchPrimaryProfileSessions(
    new URLSearchParams({ profile: 'default', limit: '20' }),
    async (profile, path) => {
      calls.push({ profile, path })

      return expected
    }
  )

  assert.deepEqual(calls, [{ profile: null, path: '/api/profiles/sessions?profile=default&limit=20' }])
  assert.equal(result, expected)
})

test('primary session reads preserve the empty-list fallback', async () => {
  const result = await fetchPrimaryProfileSessions(new URLSearchParams({ profile: 'all' }), async () => {
    throw new Error('remote unavailable')
  })

  assert.deepEqual(result, { sessions: [], total: 0, profile_totals: {} })
})

test('remote session reads split oversized sidebar windows into API-safe pages', async () => {
  const calls: Array<{ profile: string | null; path: string }> = []
  const rows = Array.from({ length: 250 }, (_, index) => ({ id: `session-${index}` }))

  const result = await fetchRemoteProfileSessions(
    'remote-work',
    new URLSearchParams({ profile: 'remote-work', limit: '300', offset: '0', order: 'updated' }),
    async (profile, path) => {
      calls.push({ profile, path })
      const url = new URL(path, 'http://desktop.test')
      const limit = Number(url.searchParams.get('limit'))
      const offset = Number(url.searchParams.get('offset'))

      if (limit > 100) {
        throw new Error(`remote /api/sessions rejects limit ${limit}`)
      }

      return {
        sessions: rows.slice(offset, offset + limit),
        total: rows.length,
        limit,
        offset
      }
    }
  )

  assert.deepEqual(calls, [
    { profile: 'remote-work', path: '/api/profiles/sessions?profile=remote-work&limit=100&offset=0&order=updated' },
    { profile: 'remote-work', path: '/api/profiles/sessions?profile=remote-work&limit=100&offset=100&order=updated' },
    { profile: 'remote-work', path: '/api/profiles/sessions?profile=remote-work&limit=50&offset=200&order=updated' }
  ])
  assert.equal(result.sessions.length, 250)
  assert.equal(result.total, 250)
  assert.equal(result.limit, 300)
  assert.equal(result.offset, 0)
  assert.deepEqual(
    result.sessions.map(row => (row as { id: string }).id),
    rows.map(row => row.id)
  )
})

test('remote paging preserves offsets and deduplicates pinned backfill rows', async () => {
  const calls: string[] = []

  const rows = Array.from({ length: 240 }, (_, index) => ({
    id: `session-${index}`,
    pinned: index === 20 || index === 200
  }))

  const pinned = rows.filter(row => row.pinned)

  const result = await fetchRemoteProfileSessions(
    'remote-work',
    new URLSearchParams({ profile: 'remote-work', limit: '150', offset: '80' }),
    async (_profile, path) => {
      calls.push(path)
      const url = new URL(path, 'http://desktop.test')
      const limit = Number(url.searchParams.get('limit'))
      const offset = Number(url.searchParams.get('offset'))
      const window = rows.slice(offset, offset + limit)
      const windowIds = new Set(window.map(row => row.id))

      return {
        sessions: [...window, ...pinned.filter(row => !windowIds.has(row.id))],
        total: rows.length,
        limit,
        offset
      }
    }
  )

  assert.deepEqual(calls, [
    '/api/profiles/sessions?profile=remote-work&limit=100&offset=80',
    '/api/profiles/sessions?profile=remote-work&limit=50&offset=180'
  ])
  assert.deepEqual(
    result.sessions.map(row => (row as { id: string }).id),
    [...rows.slice(80, 230).map(row => row.id), 'session-20']
  )
})

test('remote paging treats malformed totals as unknown instead of truncating the result', async () => {
  const rows = Array.from({ length: 250 }, (_, index) => ({ id: `session-${index}` }))

  for (const malformedTotal of [null, '', false, 100.5]) {
    const calls: string[] = []

    const result = await fetchRemoteProfileSessions(
      'remote-work',
      new URLSearchParams({ limit: '300', offset: '0' }),
      async (_profile, path) => {
        calls.push(path)
        const url = new URL(path, 'http://desktop.test')
        const limit = Number(url.searchParams.get('limit'))
        const offset = Number(url.searchParams.get('offset'))

        return {
          sessions: rows.slice(offset, offset + limit),
          total: malformedTotal,
          limit,
          offset
        }
      }
    )

    assert.deepEqual(calls, [
      '/api/profiles/sessions?limit=100&offset=0&profile=remote-work',
      '/api/profiles/sessions?limit=100&offset=100&profile=remote-work',
      '/api/profiles/sessions?limit=100&offset=200&profile=remote-work'
    ])
    assert.equal(result.sessions.length, 250)
    assert.equal(result.total, 250)
  }
})

test('merged profile windows retain pinned rows outside the recency window', () => {
  const rows = [
    { id: 'recent-default', profile: 'default', pinned: false },
    { id: 'shared-id', profile: 'default', pinned: false },
    { id: 'recent-remote', profile: 'remote-work', pinned: false },
    { id: 'shared-id', profile: 'remote-work', pinned: true },
    { id: 'old-remote', profile: 'remote-work', pinned: true },
    { id: 'old-unpinned', profile: 'remote-work', pinned: false }
  ]

  assert.deepEqual(mergeProfileSessionWindow(rows, 0, 3), [rows[0], rows[1], rows[2], rows[3], rows[4]])
})

test('remote session reads keep small requests on one call', async () => {
  const calls: Array<{ profile: string | null; path: string }> = []
  const expected = { sessions: [{ id: 'session-1' }], total: 1, limit: 20, offset: 0 }

  const result = await fetchRemoteProfileSessions(
    'remote-work',
    new URLSearchParams({ profile: 'remote-work', limit: '20', offset: '0' }),
    async (profile, path) => {
      calls.push({ profile, path })

      return expected
    }
  )

  assert.deepEqual(calls, [
    { profile: 'remote-work', path: '/api/profiles/sessions?profile=remote-work&limit=20&offset=0' }
  ])
  assert.equal(result, expected)
})

test('remote session reads use the named profile on a shared gateway instead of the default /api/sessions store', async () => {
  const calls: string[] = []

  const named = {
    sessions: [{ id: '20260815_095947_ac2552', title: 'Check channelsDVR guide data', profile: 'cableguy' }],
    total: 1,
    profile_totals: { cableguy: 1 }
  }

  const fallback = {
    sessions: [{ id: '20260814_045259_3566c8', title: 'Resume OPNsense 26.7.2 maintenance' }],
    total: 1
  }

  const result = await fetchRemoteProfileSessions(
    'cableguy',
    new URLSearchParams({ profile: 'cableguy', limit: '20', offset: '0', min_messages: '1', archived: 'exclude' }),
    async (_profile, path) => {
      calls.push(path)

      return path.startsWith('/api/profiles/sessions') ? named : fallback
    }
  )

  assert.equal(calls.some(path => path.startsWith('/api/profiles/sessions')), true)
  assert.equal(
    calls.some(path => path.startsWith('/api/sessions')),
    false,
    'must not leak the shared host default store once the named profile answers'
  )
  assert.deepEqual(
    result.sessions.map(row => (row as { id: string }).id),
    ['20260815_095947_ac2552']
  )
})

test('remote session reads keep a 200 named list even when profile_totals omits the name', async () => {
  const calls: string[] = []

  const result = await fetchRemoteProfileSessions(
    'cableguy',
    new URLSearchParams({ profile: 'cableguy', limit: '20', offset: '0' }),
    async (_profile, path) => {
      calls.push(path)

      if (path.startsWith('/api/profiles/sessions')) {
        return { sessions: [], total: 0, profile_totals: {} }
      }

      return { sessions: [{ id: 'default-opnsense' }], total: 1 }
    }
  )

  assert.deepEqual(calls, ['/api/profiles/sessions?profile=cableguy&limit=20&offset=0'])
  assert.deepEqual(result.sessions, [])
  assert.equal(result.total, 0)
})

test('remote session reads do not fall back to /api/sessions when the named list returns 5xx', async () => {
  const calls: string[] = []

  await assert.rejects(
    () =>
      fetchRemoteProfileSessions(
        'cableguy',
        new URLSearchParams({ profile: 'cableguy', limit: '20' }),
        async (_profile, path) => {
          calls.push(path)

          if (path.startsWith('/api/profiles/sessions')) {
            throw new Error('500: boom')
          }

          return { sessions: [{ id: 'default-opnsense' }], total: 1 }
        }
      ),
    /500: boom/
  )

  assert.deepEqual(calls, ['/api/profiles/sessions?profile=cableguy&limit=20'])
})

test('remote session reads do not leak the default store when the named profile is unknown', async () => {
  const calls: string[] = []

  await assert.rejects(
    () =>
      fetchRemoteProfileSessions(
        'cableguy',
        new URLSearchParams({ profile: 'cableguy', limit: '20' }),
        async (_profile, path) => {
          calls.push(path)

          if (path.startsWith('/api/profiles/sessions')) {
            throw new Error('404: {"detail":"Profile \'cableguy\' does not exist."}')
          }

          return { sessions: [{ id: 'default-opnsense' }], total: 1 }
        }
      ),
    /Profile 'cableguy' does not exist/
  )

  assert.deepEqual(calls, ['/api/profiles/sessions?profile=cableguy&limit=20'])
})

test('remote session reads fall back to /api/sessions when the named list endpoint is missing', async () => {
  const calls: string[] = []
  const fallback = { sessions: [{ id: 'dedicated-1' }], total: 1 }

  const result = await fetchRemoteProfileSessions(
    'remote-work',
    new URLSearchParams({ profile: 'remote-work', limit: '20', offset: '0' }),
    async (_profile, path) => {
      calls.push(path)

      if (path.startsWith('/api/profiles/sessions')) {
        throw new Error('404: {"detail":"No such API endpoint: /api/profiles/sessions"}')
      }

      return fallback
    }
  )

  assert.equal(calls[0], '/api/profiles/sessions?profile=remote-work&limit=20&offset=0')
  assert.equal(
    calls.some(path => path.startsWith('/api/sessions')),
    true
  )
  assert.equal(result, fallback)
})

test('remote session reads try scoped /api/sessions before the unscoped default store', async () => {
  const calls: string[] = []
  const scoped = { sessions: [{ id: 'named-1' }], total: 1 }
  const unscoped = { sessions: [{ id: 'default-opnsense' }], total: 1 }

  const result = await fetchRemoteProfileSessions(
    'cableguy',
    new URLSearchParams({ profile: 'cableguy', limit: '20', offset: '0' }),
    async (_profile, path) => {
      calls.push(path)

      if (path.startsWith('/api/profiles/sessions')) {
        throw new Error('404: {"detail":"No such API endpoint: /api/profiles/sessions"}')
      }

      return path.includes('profile=cableguy') ? scoped : unscoped
    }
  )

  assert.deepEqual(calls, [
    '/api/profiles/sessions?profile=cableguy&limit=20&offset=0',
    '/api/sessions?profile=cableguy&limit=20&offset=0'
  ])
  assert.equal(result, scoped)
})

test('remote session reads keep an empty named profile instead of leaking the default store', async () => {
  const result = await fetchRemoteProfileSessions(
    'cableguy',
    new URLSearchParams({ profile: 'cableguy', limit: '20' }),
    async (_profile, path) => {
      if (path.startsWith('/api/profiles/sessions')) {
        return { sessions: [], total: 0, profile_totals: { cableguy: 0 } }
      }

      return { sessions: [{ id: 'default-opnsense' }], total: 1 }
    }
  )

  assert.deepEqual(result.sessions, [])
  assert.equal(result.total, 0)
})

test('shared-gateway detection is URL identity, not profile name', () => {
  const remotes = {
    cableguy: { url: 'http://10.42.94.4:9119/' },
    'ubuntu-server': { url: 'http://10.42.94.4:9119' },
    dedicated: { url: 'http://10.42.94.38:9119' }
  }

  assert.equal(remoteProfileSharesGateway('cableguy', remotes), true)
  assert.equal(remoteProfileSharesGateway('ubuntu-server', remotes), true)
  assert.equal(remoteProfileSharesGateway('dedicated', remotes), false)
  assert.equal(remoteProfileSharesGateway('missing', remotes), false)
})
