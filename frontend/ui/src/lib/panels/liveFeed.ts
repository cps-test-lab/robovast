// LiveRunFeed: one run's live subscription -- the rows of its tables as its recording grows, pushed
// by the service over SSE (`GET /data/campaigns/{id}/live?run=<config>/<run>&tables=a,b`).
//
// One EventSource per run, carrying the union of every table some reader asked for. The route
// takes its table list on the URL, so a reader that names a table nobody named before means a new
// socket with the wider list; the old one is closed. A reader that goes away leaves its table in
// the list -- reopening for a shrinking set would cost a gap for the readers still there.
//
// The stream is not resumable: the server pushes what is decoded from the moment a socket is open,
// and a socket that reopens -- the browser's own retry after a dropped connection, the wider list
// above, or the watchdog below replacing a zombie -- has missed whatever landed in between. Every
// reader therefore hears `gap` when that happens, and answers it by re-reading its history through
// the query route before following again. That is the whole protocol: SQL for what is there, the
// stream for what comes, and a gap sends the reader back to SQL once.
//
// The watchdog is the one `lib/liveStream.ts` describes for the hooks: the servers heartbeat every
// second, so a socket that has carried nothing for `STALE_MS` is dead rather than quiet -- a
// suspended laptop, a torn-down port-forward -- and is replaced. Nothing here polls the service:
// rows arrive when the recorder writes them.

import type { DataRow } from '@robovast/panel-kit'

/** What a reader of one table hears. */
export type FeedEvent =
  /** Rows of `table`, in the order the recording gave them. */
  | { kind: 'batch'; table: string; rows: DataRow[] }
  /** The socket was (re)opened after this reader subscribed: rows may have been missed. Re-read
   *  the history through the query route, then keep following. */
  | { kind: 'gap' }
  /** The run has finished and its tables are complete: read them once more through the query
   *  route -- they are all there now -- and stop following. */
  | { kind: 'eof' }
  /** The service refused or lost the stream; `eof` follows. */
  | { kind: 'error'; message: string }

export type FeedListener = (event: FeedEvent) => void

/** The subset of EventSource this feed uses, so a test can hand it a fake. */
export interface EventSourceLike {
  readonly readyState: number
  onopen: ((ev: Event) => void) | null
  onerror: ((ev: Event) => void) | null
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void
  close(): void
}

/** The table name that subscribes to every table's batches (see `subscribe`). */
export const ANY_TABLE = '*'

/** Silence that means the socket is dead rather than idle -- the same margin as `liveStream.ts`,
 *  for the same reason: a throttled background tab must not be mistaken for a broken stream. */
export const STALE_MS = 15_000

/** One `batch` frame, parsed. Refuses anything that is not `{table, rows: [...]}`, so a frame the
 *  client cannot read surfaces as an error rather than as a panel that silently stops moving. */
export function parseBatch(data: string): { table: string; rows: DataRow[] } {
  const parsed: unknown = JSON.parse(data)
  if (
    !parsed || typeof parsed !== 'object' ||
    typeof (parsed as { table?: unknown }).table !== 'string' ||
    !Array.isArray((parsed as { rows?: unknown }).rows)
  ) {
    throw new Error('live batch: expected {"table": <name>, "rows": [...]}')
  }
  const { table, rows } = parsed as { table: string; rows: unknown[] }
  return {
    table,
    rows: rows.filter((r): r is DataRow => !!r && typeof r === 'object'),
  }
}

export class LiveRunFeed {
  private readonly listeners = new Map<string, Set<FeedListener>>()
  /** Every table some reader ever asked for: what the open socket carries. */
  private covered = new Set<string>()
  private es: EventSourceLike | null = null
  private opened = 0
  private lastFrame = 0
  private finished = false
  private closed = false
  private timer: ReturnType<typeof setInterval> | null = null

  /**
   * @param urlFor the stream URL for a table list, from the client.
   * @param open how a socket is opened; the browser's EventSource unless a test says otherwise.
   */
  constructor(
    private readonly urlFor: (tables: string[]) => string,
    private readonly open: (url: string) => EventSourceLike = (url) => new EventSource(url),
  ) {}

  /** Whether the run has finished, as far as the stream has said. */
  get done(): boolean {
    return this.finished
  }

  /** Hear one table's events. The socket opens on the first subscription and widens when a table
   *  is named that it does not carry yet. A subscriber to a run that has already finished hears
   *  `eof` at once, so a panel that mounts late still reads the finished tables.
   *
   *  `ANY_TABLE` hears every table the socket carries without adding one: for a reader that wants
   *  to know the run is growing (the clock's range) rather than any table in particular. */
  subscribe(table: string, listener: FeedListener): () => void {
    let set = this.listeners.get(table)
    if (!set) this.listeners.set(table, (set = new Set()))
    set.add(listener)
    if (this.finished) {
      queueMicrotask(() => listener({ kind: 'eof' }))
    } else if (table !== ANY_TABLE && !this.covered.has(table)) {
      this.covered.add(table)
      this.reopen()
    }
    return () => {
      set.delete(listener)
      if (!set.size) this.listeners.delete(table)
    }
  }

  /** Close the socket for good. A feed is per run, so switching runs closes one and opens another. */
  close(): void {
    this.closed = true
    this.es?.close()
    this.es = null
    this.listeners.clear()
    this.stopWatchdog()
  }

  /** Open a socket for every covered table, replacing the current one. Readers that were already
   *  covered hear `gap`: rows may have landed between the two sockets. */
  private reopen(): void {
    if (this.closed || this.finished) return
    const previous = this.es
    previous?.close()
    const wasOpen = this.opened > 0
    const tables = [...this.covered].sort()
    const es = this.open(this.urlFor(tables))
    this.es = es
    this.lastFrame = Date.now()
    const stamp = () => {
      this.lastFrame = Date.now()
    }
    let firstOpen = true
    es.onopen = () => {
      stamp()
      this.opened += 1
      // The browser's own retry reopens the same socket object: its second `open` is a new
      // connection with the same missed-rows problem as a socket this feed replaced.
      if (!firstOpen || wasOpen) this.broadcast({ kind: 'gap' })
      firstOpen = false
    }
    es.addEventListener('heartbeat', stamp)
    es.addEventListener('batch', (e) => {
      stamp()
      let batch: { table: string; rows: DataRow[] }
      try {
        batch = parseBatch(e.data)
      } catch (err) {
        this.broadcast({ kind: 'error', message: err instanceof Error ? err.message : String(err) })
        return
      }
      const event: FeedEvent = { kind: 'batch', table: batch.table, rows: batch.rows }
      this.emit(batch.table, event)
      this.emit(ANY_TABLE, event)
    })
    es.addEventListener('streamerror', (e) => {
      stamp()
      this.broadcast({ kind: 'error', message: String(e.data ?? 'the live stream failed') })
    })
    es.addEventListener('eof', () => {
      this.finished = true
      es.close()
      this.es = null
      this.stopWatchdog()
      this.broadcast({ kind: 'eof' })
    })
    es.onerror = () => {
      // CLOSED is the browser giving up; the watchdog reopens it on its next tick or on the tab
      // coming back. Anything else is a retry the browser is already making.
    }
    this.startWatchdog()
  }

  private emit(table: string, event: FeedEvent): void {
    const set = this.listeners.get(table)
    if (!set) return
    for (const listener of [...set]) listener(event)
  }

  private broadcast(event: FeedEvent): void {
    for (const table of [...this.listeners.keys()]) this.emit(table, event)
  }

  /** A socket that is closed, or silent past the heartbeat margin, is replaced. Checked on a timer
   *  and whenever the tab becomes visible -- the moment staleness shows. Guarded for a runtime
   *  without a document (tests). */
  private check = (): void => {
    if (this.closed || this.finished || !this.es) return
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
    const silent = Date.now() - this.lastFrame > STALE_MS
    // 2 is EventSource.CLOSED; the constant is not in scope for a runtime without EventSource.
    if (this.es.readyState === 2 || silent) this.reopen()
  }

  private startWatchdog(): void {
    if (this.timer != null) return
    this.timer = setInterval(this.check, STALE_MS)
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this.check)
    }
  }

  private stopWatchdog(): void {
    if (this.timer != null) clearInterval(this.timer)
    this.timer = null
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', this.check)
    }
  }
}
