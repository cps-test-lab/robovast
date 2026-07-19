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

/** A compact "used/capacity" resource label, e.g. "cpu 6/16 · mem 22/62 GiB". */
export function formatUsageLabel(u: {
  cpu_capacity: number
  cpu_used: number
  memory_capacity_bytes: number
  memory_used_bytes: number
}): string {
  const cpu = `cpu ${formatCores(u.cpu_used)}/${formatCores(u.cpu_capacity)}`
  const mem = `mem ${bytesToGiB(u.memory_used_bytes).toFixed(0)}/${bytesToGiB(
    u.memory_capacity_bytes,
  ).toFixed(0)} GiB`
  return `${cpu} · ${mem}`
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
