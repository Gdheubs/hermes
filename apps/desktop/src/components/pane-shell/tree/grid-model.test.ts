import { describe, expect, it } from 'vitest'

import { type GridLayout, initColumns, isGridValid, modelToZones, MULTIPLIER } from './grid-model'

/**
 * `modelToZones` sizes five scratch arrays from a zone count derived by scanning
 * `cellChildMap`. `new Array(n)` throws a hard `RangeError: Invalid array
 * length` for negative / fractional / NaN n, and because the call happens
 * during render, the throw takes down the whole desktop shell in a remount
 * loop. Malformed layouts must leave through the documented `null` return that
 * every caller already handles.
 */
describe('modelToZones rejects malformed grids instead of throwing', () => {
  const cases: Array<{ name: string; model: GridLayout }> = [
    {
      name: 'zero rows',
      model: { rows: 0, columns: 1, rowPercents: [], columnPercents: [MULTIPLIER], cellChildMap: [] }
    },
    {
      name: 'negative rows',
      model: { rows: -2, columns: 1, rowPercents: [], columnPercents: [MULTIPLIER], cellChildMap: [] }
    },
    {
      name: 'fractional columns',
      model: { rows: 1, columns: 1.5, rowPercents: [MULTIPLIER], columnPercents: [MULTIPLIER], cellChildMap: [[0]] }
    },
    {
      name: 'NaN rows',
      model: { rows: NaN, columns: 1, rowPercents: [MULTIPLIER], columnPercents: [MULTIPLIER], cellChildMap: [[0]] }
    },
    {
      name: 'Infinity columns',
      model: {
        rows: 1,
        columns: Number.POSITIVE_INFINITY,
        rowPercents: [MULTIPLIER],
        columnPercents: [MULTIPLIER],
        cellChildMap: [[0]]
      }
    },
    {
      name: 'a NaN cell index',
      model: {
        rows: 1,
        columns: 2,
        rowPercents: [MULTIPLIER],
        columnPercents: [5000, 5000],
        cellChildMap: [[0, NaN]]
      }
    },
    {
      name: 'a negative cell index',
      model: {
        rows: 1,
        columns: 2,
        rowPercents: [MULTIPLIER],
        columnPercents: [5000, 5000],
        cellChildMap: [[0, -1]]
      }
    },
    {
      name: 'a fractional cell index',
      model: {
        rows: 1,
        columns: 2,
        rowPercents: [MULTIPLIER],
        columnPercents: [5000, 5000],
        cellChildMap: [[0, 0.5]]
      }
    },
    {
      // The regression that produced the shipped crash: a row shorter than
      // `columns` reads `undefined`, Math.max yields NaN, and every NaN
      // comparison in the `zoneCount > rows * cols` bound check is false — so
      // the malformed count flowed straight into `new Array(NaN)`.
      name: 'a row shorter than columns',
      model: {
        rows: 2,
        columns: 2,
        rowPercents: [5000, 5000],
        columnPercents: [5000, 5000],
        cellChildMap: [[0, 1], [2]]
      }
    },
    {
      name: 'a missing row',
      model: {
        rows: 2,
        columns: 1,
        rowPercents: [5000, 5000],
        columnPercents: [MULTIPLIER],
        cellChildMap: [[0]]
      }
    },
    {
      name: 'a non-array row',
      model: {
        rows: 1,
        columns: 1,
        rowPercents: [MULTIPLIER],
        columnPercents: [MULTIPLIER],
        cellChildMap: [null as unknown as number[]]
      }
    },
    {
      name: 'a non-array cellChildMap',
      model: {
        rows: 1,
        columns: 1,
        rowPercents: [MULTIPLIER],
        columnPercents: [MULTIPLIER],
        cellChildMap: null as unknown as number[][]
      }
    }
  ]

  for (const { name, model } of cases) {
    it(`returns null for ${name}`, () => {
      expect(() => modelToZones(model)).not.toThrow()
      expect(modelToZones(model)).toBeNull()
    })
  }

  it('does not throw for any malformed case, including via isGridValid', () => {
    // isGridValid delegates to modelToZones, so it must stay throw-free too.
    for (const { model } of cases) {
      expect(() => isGridValid(model)).not.toThrow()
      expect(isGridValid(model)).toBe(false)
    }
  })
})

describe('modelToZones still accepts well-formed grids', () => {
  it('maps a valid single-row grid to one zone per column', () => {
    const zones = modelToZones(initColumns(3))

    expect(zones).not.toBeNull()
    expect(zones).toHaveLength(3)
  })

  it('accepts a zone spanning multiple cells', () => {
    const model: GridLayout = {
      rows: 2,
      columns: 2,
      rowPercents: [5000, 5000],
      columnPercents: [5000, 5000],
      // Zone 0 spans the whole top row; zones 1 and 2 sit below it.
      cellChildMap: [
        [0, 0],
        [1, 2]
      ]
    }

    expect(modelToZones(model)).toHaveLength(3)
    expect(isGridValid(model)).toBe(true)
  })

  it('validates the templates the shell boots with', () => {
    for (const count of [1, 2, 3, 4, 5, 6]) {
      expect(isGridValid(initColumns(count))).toBe(true)
    }
  })
})
