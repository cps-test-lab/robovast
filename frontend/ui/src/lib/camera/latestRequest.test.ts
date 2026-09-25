import { describe, expect, it } from 'vitest'

import { LatestRequest } from './latestRequest'

/** Fake timers and a fake clock the scheduler is driven with. */
function harness(opts: { debounceMs?: number; minIntervalMs?: number } = {}) {
  let now = 0
  const timers: { at: number; fn: () => void }[] = []
  const started: number[] = []
  const results: [number, string | Error][] = []
  const resolvers = new Map<number, (v: string) => void>()
  const rejecters = new Map<number, (e: Error) => void>()

  const req = new LatestRequest<number, string>({
    ...opts,
    run: (key) =>
      new Promise<string>((resolve, reject) => {
        started.push(key)
        resolvers.set(key, resolve)
        rejecters.set(key, reject)
      }),
    onResult: (key, r) => results.push([key, r]),
    now: () => now,
    setTimeout: (fn, ms) => {
      const timer = { at: now + ms, fn }
      timers.push(timer)
      return timer
    },
    clearTimeout: (id) => {
      const i = timers.indexOf(id as { at: number; fn: () => void })
      if (i >= 0) timers.splice(i, 1)
    },
  })

  /** Advance the fake clock, firing every timer due by then. */
  const advance = (ms: number) => {
    const until = now + ms
    for (;;) {
      const due = timers.filter((t) => t.at <= until).sort((a, b) => a.at - b.at)[0]
      if (!due) break
      timers.splice(timers.indexOf(due), 1)
      now = due.at
      due.fn()
    }
    now = until
  }
  const resolve = async (key: number, v = `img${key}`) => {
    resolvers.get(key)!(v)
    await Promise.resolve()
    await Promise.resolve()
  }
  const reject = async (key: number, e: Error) => {
    rejecters.get(key)!(e)
    await Promise.resolve()
    await Promise.resolve()
  }
  return { req, advance, resolve, reject, started, results, timers }
}

describe('LatestRequest', () => {
  it('debounces a scrub to one fetch of the last key', () => {
    const h = harness()
    h.req.request(1, { debounceMs: 100 })
    h.advance(50)
    h.req.request(2, { debounceMs: 100 })
    h.advance(50)
    h.req.request(3, { debounceMs: 100 })
    expect(h.started).toEqual([])
    h.advance(100)
    expect(h.started).toEqual([3])
  })

  it('keeps one request in flight and follows it with the newest key only', async () => {
    const h = harness()
    h.req.request(1)
    h.advance(0)
    expect(h.started).toEqual([1])
    h.req.request(2)
    h.req.request(3)
    h.advance(1000)
    expect(h.started).toEqual([1])
    await h.resolve(1)
    expect(h.results).toEqual([[1, 'img1']])
    h.advance(0)
    expect(h.started).toEqual([1, 3])
  })

  it('does not refetch the delivered key, nor the one in flight', async () => {
    const h = harness()
    h.req.request(1)
    h.advance(0)
    h.req.request(1)
    await h.resolve(1)
    h.req.request(1)
    h.advance(1000)
    expect(h.started).toEqual([1])
    expect(h.results).toHaveLength(1)
  })

  it('bounds the rate to one start per minIntervalMs while playing', async () => {
    const h = harness({ minIntervalMs: 100 })
    h.req.request(1)
    h.advance(0)
    await h.resolve(1)
    h.req.request(2)
    h.advance(30)
    expect(h.started).toEqual([1])
    h.advance(70)
    expect(h.started).toEqual([1, 2])
  })

  it('refetches with force, and after invalidate', async () => {
    const h = harness()
    h.req.request(1)
    h.advance(0)
    await h.resolve(1)
    h.req.request(1, { force: true })
    h.advance(0)
    expect(h.started).toEqual([1, 1])
    await h.resolve(1)
    h.req.invalidate()
    h.req.request(1)
    h.advance(0)
    expect(h.started).toEqual([1, 1, 1])
  })

  it('delivers a failed fetch as its error', async () => {
    const h = harness()
    h.req.request(4)
    h.advance(0)
    await h.reject(4, new Error('no frame'))
    expect(h.results).toEqual([[4, new Error('no frame')]])
  })

  it('delivers nothing after dispose', async () => {
    const h = harness()
    h.req.request(1)
    h.advance(0)
    h.req.dispose()
    await h.resolve(1)
    expect(h.results).toEqual([])
    h.req.request(2)
    h.advance(1000)
    expect(h.started).toEqual([1])
  })
})
