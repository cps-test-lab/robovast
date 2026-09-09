import { describe, expect, it } from 'vitest'
import { CHIP_COLOURS, distinctColorer, slotOf } from './nameColor'

const PALETTE = ['a', 'b', 'c', 'd'] as const

describe('slotOf', () => {
  it('gives the same name the same slot every time', () => {
    // The whole basis of deriving rather than storing a colour: nothing is coordinated
    // between viewers or sessions, so the hash has to be the agreement.
    expect(slotOf('worker-a', 8)).toBe(slotOf('worker-a', 8))
  })

  it('stays inside the palette', () => {
    for (const n of ['', 'x', 'a-much-longer-name', '☃'])
      expect(slotOf(n, 4)).toBeGreaterThanOrEqual(0), expect(slotOf(n, 4)).toBeLessThan(4)
  })
})

describe('distinctColorer', () => {
  it('keeps colliding names apart', () => {
    // The reason this exists rather than a bare hash: two things painted identically read as
    // one thing. `sut` and `robovast` are the pair that proved it in the log panel.
    const colour = distinctColorer(['sut', 'robovast'], CHIP_COLOURS)
    expect(colour('sut')).not.toBe(colour('robovast'))
  })

  it('gives every name its own colour while the palette lasts', () => {
    const names = ['n1', 'n2', 'n3', 'n4']
    const colour = distinctColorer(names, PALETTE)
    expect(new Set(names.map(colour)).size).toBe(names.length)
  })

  it('does not recolour earlier names when a later one appears', () => {
    // A job list grows as pods are scheduled. If adding a row repainted the rows above it,
    // the colour would stop meaning "this machine" and start meaning "this render".
    const before = distinctColorer(['n1', 'n2'], PALETTE)
    const after = distinctColorer(['n1', 'n2', 'n3'], PALETTE)
    // n3 sorts last, so it can only take a slot the other two did not want.
    expect(after('n1')).toBe(before('n1'))
    expect(after('n2')).toBe(before('n2'))
  })

  it('is independent of the order the names arrive in', () => {
    const a = distinctColorer(['n3', 'n1', 'n2'], PALETTE)
    const b = distinctColorer(['n1', 'n2', 'n3'], PALETTE)
    for (const n of ['n1', 'n2', 'n3']) expect(a(n)).toBe(b(n))
  })

  it('colours a name it was not built with rather than leaving it blank', () => {
    // A caller rendering one more row must not have to rebuild the colourer to get a colour.
    expect(distinctColorer([], PALETTE)('unseen')).toBe(PALETTE[slotOf('unseen', 4)])
  })

  it('repeats a colour rather than dropping one when the palette runs out', () => {
    const names = ['n1', 'n2', 'n3', 'n4', 'n5']
    const colour = distinctColorer(names, PALETTE)
    expect(names.every((n) => PALETTE.includes(colour(n) as (typeof PALETTE)[number]))).toBe(true)
  })
})
