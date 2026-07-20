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
