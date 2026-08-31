import { describe, expect, it } from 'vitest'
import {
  addToast,
  dismissToast,
  expireToasts,
  extendDeadlines,
  isFailure,
  ERROR_DURATION_MS,
  MAX_VISIBLE,
  MAX_VISIBLE_ERRORS,
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

// A failure clears itself like everything else, but it is given weight: longer on screen,
// counted apart so a burst cannot evict it, drawn nearest the corner.
describe('a failure is given weight, not permanence', () => {
  const boom = (over: Partial<ToastSpec> = {}): ToastSpec =>
    ({ severity: 'error', message: 'Retrigger failed.', ...over })

  it('gets longer on screen than a passing notice', () => {
    // Ten seconds is enough to notice a sentence and not enough to read one, so the shared
    // clock would hide the very text a failure exists to deliver.
    const [t] = addToast([], boom(), 1_000, 1)
    expect(t.deadline).toBe(1_000 + ERROR_DURATION_MS)
    expect(ERROR_DURATION_MS).toBeGreaterThan(DEFAULT_DURATION_MS)
    expect(isFailure(t)).toBe(true)
  })

  it('still clears itself, so nothing has to be clicked away', () => {
    const list = addToast([], boom(), 0, 1)
    expect(expireToasts(list, ERROR_DURATION_MS + 1)).toHaveLength(0)
  })

  it('outlives a passing notice raised at the same moment', () => {
    let list = addToast([], spec(), 0, 1)
    list = addToast(list, boom(), 0, 2)
    const later = expireToasts(list, DEFAULT_DURATION_MS + 1)
    expect(later.map((t) => t.id)).toEqual([2])
  })

  it('goes early when dismissed', () => {
    const list = addToast([], boom(), 0, 1)
    expect(dismissToast(list, 1)).toHaveLength(0)
  })

  it('is held by the hover pause like everything else', () => {
    const list = extendDeadlines(addToast([], boom(), 0, 1), 250)
    expect(list[0].deadline).toBe(ERROR_DURATION_MS + 250)
  })

  it('is not evicted by a burst of transient notices', () => {
    // The failure mode worth naming: one shared cap means a handful of campaigns ending can
    // silently carry off the refusal someone has not read yet.
    let list = addToast([], boom(), 0, 1)
    for (let i = 0; i < MAX_VISIBLE * 3; i++) {
      list = addToast(list, spec({ message: `m${i}` }), 0, 100 + i)
    }
    expect(list.filter(isFailure)).toHaveLength(1)
    expect(list.filter((t) => !isFailure(t))).toHaveLength(MAX_VISIBLE)
  })

  it('does not crowd out passing notices either, having its own cap', () => {
    let list: Toast[] = []
    for (let i = 0; i < MAX_VISIBLE_ERRORS + 2; i++) {
      list = addToast(list, boom({ message: `e${i}` }), 0, i + 1)
    }
    expect(list).toHaveLength(MAX_VISIBLE_ERRORS)
    expect(list.map((t) => t.message)).toEqual(['e2', 'e3', 'e4'])   // oldest dropped
  })

  it('is replaced in place when the same action fails again', () => {
    const first = addToast([], boom({ key: 'retrigger:c1' }), 0, 1)
    const again = addToast(first, boom({ key: 'retrigger:c1', message: 'again' }), 5_000, 2)
    expect(again).toHaveLength(1)
    expect(again[0].message).toBe('again')
    expect(again[0].deadline).toBe(5_000 + ERROR_DURATION_MS)
  })

  it('re-caps when a key moves a toast between the two groups', () => {
    // A retry reuses the key its success used, so a replacement can move a toast between the
    // two groups — and the group it lands in can then be over its cap.
    let list: Toast[] = []
    for (let i = 0; i < MAX_VISIBLE_ERRORS; i++) {
      list = addToast(list, boom({ message: `e${i}` }), 0, i + 1)
    }
    list = addToast(list, spec({ key: 'k', message: 'ok' }), 0, 90)
    list = addToast(list, boom({ key: 'k', message: 'now failed' }), 0, 91)
    expect(list.filter(isFailure)).toHaveLength(MAX_VISIBLE_ERRORS)
    expect(list.some((t) => t.message === 'now failed')).toBe(true)
  })
})
