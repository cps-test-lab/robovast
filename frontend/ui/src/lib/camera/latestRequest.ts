// LatestRequest: the fetch discipline behind a picture that follows a clock.
//
// A clock emits at display rate while playing and on every pointer move while scrubbing; a frame
// route answers in tens of milliseconds and a render in seconds. Between the two sits this: at most
// one request in flight, only ever for the newest key asked for, started no sooner than `debounceMs`
// after the last change and no more often than `minIntervalMs` -- and never for a key whose
// answer is the one already on screen. Every earlier key is simply dropped: a frame nobody is
// looking at any more is not worth the bytes.
//
// Timers and the clock are injectable so the schedule is testable without waiting through it.

export interface LatestRequestOptions<K, V> {
  /** Fetch one key. The signal aborts when the request is discarded (`dispose`). */
  run: (key: K, signal: AbortSignal) => Promise<V>
  /** Called with the answer for `key`, or the error the fetch threw, in request order. */
  onResult: (key: K, result: V | Error) => void
  /** Quiet time after the last `request` before a fetch starts. */
  debounceMs?: number
  /** The least time between two fetch starts. */
  minIntervalMs?: number
  /** Key equality; keys that compare equal share one fetch. */
  same?: (a: K, b: K) => boolean
  now?: () => number
  setTimeout?: (fn: () => void, ms: number) => unknown
  clearTimeout?: (id: unknown) => void
}

export class LatestRequest<K, V> {
  private readonly opts: Required<Pick<LatestRequestOptions<K, V>, 'run' | 'onResult'>> &
    LatestRequestOptions<K, V>
  private pending: { key: K } | null = null
  private inFlight: { key: K; controller: AbortController } | null = null
  /** The key whose answer `onResult` delivered last -- what is on screen. */
  private delivered: { key: K } | null = null
  private timer: unknown = null
  private lastStart = -Infinity
  private disposed = false

  constructor(opts: LatestRequestOptions<K, V>) {
    this.opts = opts
  }

  private get same() {
    return this.opts.same ?? ((a: K, b: K) => a === b)
  }
  private get now() {
    return this.opts.now ?? (() => Date.now())
  }
  private get setTimer() {
    return this.opts.setTimeout ?? ((fn: () => void, ms: number) => setTimeout(fn, ms))
  }
  private get clearTimer() {
    return this.opts.clearTimeout ?? ((id: unknown) => clearTimeout(id as number))
  }

  /** Ask for `key`. A key that is already delivered, or already being fetched, costs nothing
   *  unless `force` says the answer is to be fetched again (a newest-state render). `debounceMs`
   *  overrides the instance's quiet time for this change: 0 while playing, longer while scrubbing. */
  request(key: K, opts: { force?: boolean; debounceMs?: number } = {}): void {
    if (this.disposed) return
    if (!opts.force) {
      if (this.inFlight && this.same(this.inFlight.key, key)) {
        this.pending = null
        return
      }
      if (!this.inFlight && this.delivered && this.same(this.delivered.key, key)) {
        this.pending = null
        this.cancelTimer()
        return
      }
    }
    this.pending = { key }
    if (this.inFlight) return // started when it lands
    this.schedule(opts.debounceMs ?? this.opts.debounceMs ?? 0)
  }

  /** Whether a fetch is running. */
  get busy(): boolean {
    return this.inFlight !== null
  }

  /** Forget the delivered key, so the next `request` for it fetches again. */
  invalidate(): void {
    this.delivered = null
  }

  dispose(): void {
    this.disposed = true
    this.cancelTimer()
    this.pending = null
    this.inFlight?.controller.abort()
    this.inFlight = null
  }

  private cancelTimer() {
    if (this.timer !== null) this.clearTimer(this.timer)
    this.timer = null
  }

  private schedule(debounceMs: number) {
    this.cancelTimer()
    const minGap = this.opts.minIntervalMs ?? 0
    const wait = Math.max(debounceMs, this.lastStart + minGap - this.now(), 0)
    this.timer = this.setTimer(() => {
      this.timer = null
      this.start()
    }, wait)
  }

  private start() {
    const next = this.pending
    this.pending = null
    if (!next || this.disposed) return
    const controller = new AbortController()
    this.inFlight = { key: next.key, controller }
    this.lastStart = this.now()
    void this.opts.run(next.key, controller.signal).then(
      (v) => this.land(next.key, controller, v),
      (e: unknown) => this.land(next.key, controller, e instanceof Error ? e : new Error(String(e))),
    )
  }

  private land(key: K, controller: AbortController, result: V | Error) {
    if (this.disposed || this.inFlight?.controller !== controller) return
    this.inFlight = null
    this.delivered = { key }
    this.opts.onResult(key, result)
    if (this.pending) this.schedule(0)
  }
}
