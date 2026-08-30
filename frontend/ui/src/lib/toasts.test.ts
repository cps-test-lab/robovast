import { describe, expect, it } from 'vitest'
import {
  addToast,
  dismissToast,
  expireToasts,
  extendDeadlines,
  MAX_VISIBLE,
  DEFAULT_DURATION_MS,
  type Toast,
  type ToastSpec,
} from './toasts'

const spec = (s: Partial<ToastSpec> = {}): ToastSpec =>
  ({ severity: 'success', message: 'Done', ...s })

/** Add n distinct toasts starting at t=0, ids 1..n. */
const build = (n: number): Toast[] => {
  let list: Toast[] = []
  for (let i = 1; i <= n; i++) list = addToast(list, spec({ message: `m${i}` }), 0, i)
  return list
}

describe('addToast', () => {
  it('stamps a deadline one full lifetime out', () => {
    const [t] = addToast([], spec(), 1_000, 1)
    expect(t.deadline).toBe(1_000 + DEFAULT_DURATION_MS)
  })

  it('stacks unkeyed toasts, because they are separate events', () => {
    const list = addToast(addToast([], spec(), 0, 1), spec(), 0, 2)
    expect(list.map((t) => t.id)).toEqual([1, 2])
  })

  it('replaces a keyed toast instead of stacking a duplicate', () => {
    const first = addToast([], spec({ key: 'retrigger:c1', message: 'first' }), 0, 1)
    const again = addToast(first, spec({ key: 'retrigger:c1', message: 'second' }), 500, 2)
    expect(again).toHaveLength(1)
    expect(again[0].message).toBe('second')
    // The id is inherited so the element is updated rather than remounted mid-read.
    expect(again[0].id).toBe(1)
    expect(again[0].deadline).toBe(500 + DEFAULT_DURATION_MS)
  })

  it('keeps a replaced toast in place, since position means age', () => {
    let list = addToast([], spec({ key: 'k', message: 'keyed' }), 0, 1)
    list = addToast(list, spec({ message: 'later' }), 0, 2)
    list = addToast(list, spec({ key: 'k', message: 'refreshed' }), 0, 3)
    expect(list.map((t) => t.message)).toEqual(['refreshed', 'later'])
  })

  it('treats different keys as different toasts', () => {
    const list = addToast(addToast([], spec({ key: 'a' }), 0, 1), spec({ key: 'b' }), 0, 2)
    expect(list).toHaveLength(2)
  })

  it('drops the oldest past MAX_VISIBLE, so a burst cannot cover the page', () => {
    const list = build(MAX_VISIBLE + 2)
    expect(list).toHaveLength(MAX_VISIBLE)
    expect(list[0].message).toBe('m3')
    expect(list[MAX_VISIBLE - 1].message).toBe(`m${MAX_VISIBLE + 2}`)
  })

  it('does not trim when a keyed replacement keeps the length the same', () => {
    let list = build(MAX_VISIBLE)
    const oldest = list[0].message
    list = addToast(list, spec({ key: 'fresh' }), 0, 99)
    expect(list).toHaveLength(MAX_VISIBLE)
    // The keyed one displaced the oldest; replacing it again must not displace another.
    list = addToast(list, spec({ key: 'fresh', message: 'again' }), 0, 100)
    expect(list).toHaveLength(MAX_VISIBLE)
    expect(list.some((t) => t.message === oldest)).toBe(false)
  })
})

describe('expireToasts', () => {
  it('drops only what is past its deadline', () => {
    const list = [
      { id: 1, severity: 'success', message: 'gone', deadline: 100 },
      { id: 2, severity: 'success', message: 'stays', deadline: 300 },
    ] as Toast[]
    expect(expireToasts(list, 200).map((t) => t.id)).toEqual([2])
  })

  it('returns the same array when nothing expired, so a tick costs no re-render', () => {
    const list = build(2)
    expect(expireToasts(list, 1)).toBe(list)
  })

  it('expires exactly at the deadline rather than a tick later', () => {
    const [t] = addToast([], spec(), 0, 1)
    expect(expireToasts([t], t.deadline)).toHaveLength(0)
  })
})

describe('dismissToast', () => {
  it('removes one by id and leaves the rest in order', () => {
    const list = build(3)
    expect(dismissToast(list, 2).map((t) => t.id)).toEqual([1, 3])
  })

  it('returns the same array for an id that is not there', () => {
    const list = build(2)
    expect(dismissToast(list, 99)).toBe(list)
  })
})

describe('extendDeadlines', () => {
  it('pushes every deadline out, which is how hovering pauses the stack', () => {
    const list = build(2)
    const held = extendDeadlines(list, 250)
    expect(held.map((t) => t.deadline)).toEqual(list.map((t) => t.deadline + 250))
  })

  it('is a no-op for an empty list or a non-positive step', () => {
    const list = build(1)
    expect(extendDeadlines(list, 0)).toBe(list)
    expect(extendDeadlines([], 100)).toEqual([])
  })
})
