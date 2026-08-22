// Small pure formatting helpers shared by the UI. Kept dependency-free and
// side-effect-free so they are trivially testable.

/** Bytes → GiB, rounded to one decimal (e.g. 22.4). */
export function bytesToGiB(bytes: number): number {
  return bytes / 1024 ** 3
}

/** CPU cores, at most one decimal (whole numbers print without a ".0"). */
export function formatCores(cores: number): string {
  return Number.isInteger(cores) ? String(cores) : cores.toFixed(1)
}

interface Usage {
  cpu_capacity: number
  cpu_used: number
  memory_capacity_bytes: number
  memory_used_bytes: number
}

/** Compact "used/capacity" the sidebar CPU bar shows in-track, e.g. "10/98". */
export function formatCpuUsed(u: Usage): string {
  return `${formatCores(u.cpu_used)}/${formatCores(u.cpu_capacity)}`
}

/** Compact "used/capacity" the sidebar memory bar shows in-track, e.g. "10/20". */
export function formatMemUsed(u: Usage): string {
  return `${bytesToGiB(u.memory_used_bytes).toFixed(0)}/${bytesToGiB(
    u.memory_capacity_bytes,
  ).toFixed(0)}`
}

/** Bytes → a compact size, e.g. "912 KiB", "39.9 MiB", "1.4 GiB". */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${Math.round(bytes)} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes / 1024
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i += 1
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[i]}`
}

/** Compact "used/capacity" in ONE shared unit, e.g. "0.4/1.8 TiB", "82/512 GiB". */
export function formatBytesPair(used: number, capacity: number): string {
  // Not `formatMemUsed`'s fixed-GiB style: a node filesystem is often TiB-scale, where
  // "412/1863" is both unreadable and unlabelled. Same unit table and rounding as
  // `formatBytes`, but BOTH numbers are scaled by the CAPACITY's divisor, so the pair
  // reads as two magnitudes in one unit instead of "1.4 TiB/3.6 TiB" — twice the width,
  // in a ~124px track. A capacity below 1 KiB is not a disk, so there is no bytes case.
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let scale = 1024
  let i = 0
  while (capacity / scale >= 1024 && i < units.length - 1) {
    scale *= 1024
    i += 1
  }
  const one = (b: number) => {
    const v = b / scale
    return v < 10 ? v.toFixed(1) : String(Math.round(v))
  }
  return `${one(used)}/${one(capacity)} ${units[i]}`
}

export interface WorkProgress {
  phase: 'listing' | 'downloading' | 'executing'
  unit: 'files' | 'cells'
  done: number
  total: number | null
  bytes_done: number
  bytes_total: number | null
  detail: string
}

interface DataFetchStatus {
  fetch_required: boolean
  cached: boolean
  transfer: 'none' | 'cluster-network' | 'port-forward'
  db_bytes: number
  fetch_in_progress: boolean
  progress?: WorkProgress | null
}

/**
 * What to say while a campaign's first query runs, or `null` when there is nothing to say.
 *
 * A cluster campaign's first query fetches its databases from the object store *inside* the
 * request, which without a word is indistinguishable from a hang. `null` for a local service
 * (nothing to transfer) and for an already-cached campaign, so the common case stays silent
 * rather than explaining something that is not happening.
 *
 * When the service reports live counts they replace the generic sentence: "downloaded 152 of
 * 410 files" is the answer to the question the generic one only acknowledges. Without them
 * (an older service, or the moment before the first count lands) the original wording stands.
 *
 * Lives here, not in a view, because three of them show it and the wording must not drift.
 */
export function formatDataFetchLabel(status: DataFetchStatus | undefined): string | null {
  if (!status || !status.fetch_required || status.cached) return null
  const via = status.transfer === 'port-forward' ? ' over a port-forward' : ''
  const p = status.progress
  if (p) {
    if (p.phase === 'listing') return `Listing campaign files${via}…`
    if (p.phase === 'executing')
      return `Running notebook — cell ${p.done}${p.total ? ` of ${p.total}` : ''}…`
    // Bytes rather than the file count in the parenthetical: "410 files" says nothing about
    // how long this takes, and the transferred size is what the wait is actually made of.
    const size = p.bytes_total ? ` (${formatBytes(p.bytes_total)})` : ''
    const of = p.total ? ` of ${p.total}` : ''
    return `Downloading campaign data${size}${via} — ${p.done}${of} files…`
  }
  const size = status.db_bytes ? ` (${formatBytes(status.db_bytes)})` : ''
  if (status.fetch_in_progress) return `Fetching campaign data${size}${via}…`
  return `First query — fetching campaign data${size} from the object store${via}…`
}

/** 0-100 for a determinate bar, or `null` when the total is unknown (draw indeterminate). */
export function progressPercent(p: WorkProgress | null | undefined): number | null {
  if (!p || !p.total) return null
  return Math.min(100, Math.round((p.done / p.total) * 100))
}

/** Seconds → a coarse human duration: "45s", "12m", "2h 5m", "1d 3h". */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  const m = Math.round(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  const remM = m % 60
  if (h < 24) return remM ? `${h}h ${remM}m` : `${h}h`
  const d = Math.floor(h / 24)
  const remH = h % 24
  return remH ? `${d}d ${remH}h` : `${d}d`
}
