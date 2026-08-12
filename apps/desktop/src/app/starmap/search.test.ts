import { describe, expect, it } from 'vitest'

import type { StarmapGraph, StarmapNode } from '@/types/hermes'

import { distinctOrigins, EMPTY_FILTERS, filterNodes, hasActiveNarrowing, nodeOrigin } from './search'

const node = (over: Partial<StarmapNode> & { id: string }): StarmapNode => ({
  category: 'memory',
  createdBy: 'memory',
  kind: 'memory',
  label: over.id,
  pinned: false,
  state: 'active',
  timestamp: 1_700_000_000,
  useCount: 0,
  ...over
})

const graph = (nodes: StarmapNode[], memory: StarmapGraph['memory'] = []): StarmapGraph => ({
  clusters: [],
  edges: [],
  memory,
  nodes,
  stats: {}
})

describe('nodeOrigin / distinctOrigins', () => {
  it('defaults to hermes and normalizes case', () => {
    expect(nodeOrigin(node({ id: 'a' }))).toBe('hermes')
    expect(nodeOrigin(node({ id: 'b', origin: 'ChatGPT' }))).toBe('chatgpt')
  })

  it('lists hermes first, imports alphabetically — open-ended for future sources', () => {
    const origins = distinctOrigins([
      node({ id: 'a', origin: 'gemini' }),
      node({ id: 'b' }),
      node({ id: 'c', origin: 'chatgpt' }),
      node({ id: 'd', origin: 'chatgpt' })
    ])

    expect(origins).toEqual(['hermes', 'chatgpt', 'gemini'])
  })
})

describe('filterNodes', () => {
  const g = graph(
    [
      node({ id: 'memory:honcho:0', label: 'garden plan…', origin: 'chatgpt', timestamp: 1_600_000_000 }),
      node({ id: 'memory:memory:1', label: 'DGX cluster facts', timestamp: 1_700_000_000 }),
      node({
        category: 'devops',
        createdBy: 'agent',
        id: 'deploy-skill',
        kind: 'skill',
        label: 'deploy-skill',
        timestamp: 1_650_000_000
      }),
      node({ id: 'memory:honcho:2', label: 'undated note', timestamp: null })
    ],
    [
      { body: 'full tomato planting schedule for clay soil', source: 'honcho', title: 'garden plan…' },
      { body: 'spark cluster', source: 'memory', title: 'DGX cluster facts' }
    ] as StarmapGraph['memory']
  )

  it('returns everything chronologically when nothing narrows (undated last)', () => {
    const ids = filterNodes(g, '', EMPTY_FILTERS).map(n => n.id)

    expect(ids).toEqual(['memory:honcho:0', 'deploy-skill', 'memory:memory:1', 'memory:honcho:2'])
  })

  it('matches memory card BODIES, not just truncated labels', () => {
    const ids = filterNodes(g, 'tomato clay', EMPTY_FILTERS).map(n => n.id)

    expect(ids).toEqual(['memory:honcho:0'])
  })

  it('filters by kind, source, and date range', () => {
    expect(filterNodes(g, '', { ...EMPTY_FILTERS, kind: 'skill' }).map(n => n.id)).toEqual(['deploy-skill'])
    expect(filterNodes(g, '', { ...EMPTY_FILTERS, source: 'chatgpt' }).map(n => n.id)).toEqual(['memory:honcho:0'])
    // 2021-06-06 ≈ 1622930400 — excludes the 2020 chatgpt node, keeps 2022+2023; undated drops.
    expect(filterNodes(g, '', { ...EMPTY_FILTERS, from: '2021-06-06' }).map(n => n.id)).toEqual([
      'deploy-skill',
      'memory:memory:1'
    ])
  })

  it('requires every term (AND)', () => {
    expect(filterNodes(g, 'cluster spark', EMPTY_FILTERS).map(n => n.id)).toEqual(['memory:memory:1'])
    expect(filterNodes(g, 'cluster nonexistent', EMPTY_FILTERS)).toEqual([])
  })
})

describe('hasActiveNarrowing', () => {
  it('is false for the idle sidebar and true for any narrowing', () => {
    expect(hasActiveNarrowing('', EMPTY_FILTERS)).toBe(false)
    expect(hasActiveNarrowing('  ', EMPTY_FILTERS)).toBe(false)
    expect(hasActiveNarrowing('x', EMPTY_FILTERS)).toBe(true)
    expect(hasActiveNarrowing('', { ...EMPTY_FILTERS, kind: 'memory' })).toBe(true)
    expect(hasActiveNarrowing('', { ...EMPTY_FILTERS, source: 'chatgpt' })).toBe(true)
    expect(hasActiveNarrowing('', { ...EMPTY_FILTERS, from: '2026-01-01' })).toBe(true)
  })
})
