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

/** CPU half of the usage label, e.g. "cpu 6/16". */
export function formatCpuLabel(u: Usage): string {
  return `cpu ${formatCores(u.cpu_used)}/${formatCores(u.cpu_capacity)}`
}

/** Memory half of the usage label, e.g. "mem 22/62 GiB". */
export function formatMemLabel(u: Usage): string {
  return `mem ${bytesToGiB(u.memory_used_bytes).toFixed(0)}/${bytesToGiB(
    u.memory_capacity_bytes,
  ).toFixed(0)} GiB`
}

/** CPU capacity only, e.g. "96 CPUs". */
export function formatCpuCapacity(u: Usage): string {
  return `${formatCores(u.cpu_capacity)} CPUs`
}

/** Memory capacity only, e.g. "32 GiB". */
export function formatMemCapacity(u: Usage): string {
  return `${bytesToGiB(u.memory_capacity_bytes).toFixed(0)} GiB`
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

/** A compact "used/capacity" resource label, e.g. "cpu 6/16 · mem 22/62 GiB". */
export function formatUsageLabel(u: Usage): string {
  return `${formatCpuLabel(u)} · ${formatMemLabel(u)}`
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

interface DataFetchStatus {
  fetch_required: boolean
  cached: boolean
  transfer: 'none' | 'cluster-network' | 'port-forward'
  db_bytes: number
  fetch_in_progress: boolean
}

/**
 * What to say while a campaign's first query runs, or `null` when there is nothing to say.
 *
 * A cluster campaign's first query fetches its databases from the object store *inside* the
 * request, which without a word is indistinguishable from a hang. `null` for a local service
 * (nothing to transfer) and for an already-cached campaign, so the common case stays silent
 * rather than explaining something that is not happening.
 *
 * Lives here, not in a view, because two of them show it and the wording must not drift.
 */
export function formatDataFetchLabel(status: DataFetchStatus | undefined): string | null {
  if (!status || !status.fetch_required || status.cached) return null
  const size = status.db_bytes ? ` (${formatBytes(status.db_bytes)})` : ''
  const via = status.transfer === 'port-forward' ? ' over a port-forward' : ''
  if (status.fetch_in_progress) return `Fetching campaign data${size}${via}…`
  return `First query — fetching campaign data${size} from the object store${via}…`
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
