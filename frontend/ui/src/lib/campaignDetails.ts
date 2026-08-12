// Client-side model for the campaign card's "Details" panel: what a campaign cost and whether
// it ran well. Pure, React-free helpers over rows fetched with `queryCampaignDataSql`, in the
// same spirit as `resultsTree.ts` — the panel is three SQL queries and arithmetic, with no new
// service field and nothing new written into a campaign's results.
//
// The panel answers "did this run well, and what did it cost?"; the Results Explorer keeps
// answering "what did it find?". That boundary is why there is no per-config breakdown here.
//
// Nothing below depends on a postprocessing plugin a campaign happens to declare: the run rows
// come from `run_view` (a temp view over the live `campaign.db`, so it works before any
// postprocessing at all) and the CPU rows from `resource_usage`, which is written by an
// **auto**-injected plugin for every campaign. CPU is therefore the one part that can be
// missing — a campaign that was never postprocessed has no `resource_usage` table — and it
// degrades to absent rather than to zero.

// ---------------------------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------------------------

/** Per-run facts: what each run did, how long it took, and which round proposed it.
 *
 *  `run_view` rather than the postprocessed `runs` table, for the same reason `resultsTree.ts`
 *  uses it: it is a view over `campaign.db`, so a campaign with no `data.db` still has rows. */
export const DETAILS_RUNS_SQL =
  'SELECT config_name, run_id, batch, status, duration_s, start_time, objective FROM run_view ' +
  'ORDER BY batch, start_time'

/** Per-container CPU, pooled over every tick of every run.
 *
 *  The inner query is the load-bearing part: one row of `resource_usage` is one PROCESS NAME,
 *  not a container, so the per-tick values must be summed before any max or percentile — a tick
 *  is concurrent demand, and the largest single process is not it. Taking MAX(cpu_percent)
 *  directly would report the busiest single process and silently under-size every container
 *  that runs more than one.
 *
 *  Five percentiles rather than one, because the panel draws the DISTRIBUTION: a single p95 says
 *  where to set the reservation but not whether the container sits there or merely visits. A
 *  container idling at 0.2 with a brief 3-core compile and one holding 3 cores throughout have
 *  the same p95 and want very different answers, and only the spread separates them. Computed in
 *  SQL rather than shipped as samples: a campaign's raw ticks run to tens of thousands of rows
 *  per container, and the box needs six numbers.
 *
 *  `in_window = 1` keeps this to the run's own window; bring-up and teardown are the job's cost.
 *
 *  PERCENTILE is registered on the service's connection (see results_processing/data_query.py),
 *  not a SQLite built-in — this query only runs through the campaign query endpoint. */
export const DETAILS_CPU_SQL =
  'SELECT container, PERCENTILE(cores, 5) AS p05, PERCENTILE(cores, 25) AS p25, ' +
  'PERCENTILE(cores, 50) AS p50, PERCENTILE(cores, 75) AS p75, PERCENTILE(cores, 95) AS p95, ' +
  'MAX(cores) AS peak, SUM(cores) AS core_seconds, COUNT(*) AS ticks, ' +
  'PERCENTILE(bytes, 5) AS m05, PERCENTILE(bytes, 25) AS m25, PERCENTILE(bytes, 50) AS m50, ' +
  'PERCENTILE(bytes, 75) AS m75, PERCENTILE(bytes, 95) AS m95, MAX(bytes) AS m_peak, ' +
  'SUM(bytes) AS byte_seconds FROM (' +
  'SELECT container, config_name, run_id, timestamp, SUM(cpu_percent)/100.0 AS cores, ' +
  'SUM(memory_rss_bytes) AS bytes ' +
  'FROM resource_usage WHERE in_window = 1 ' +
  'GROUP BY container, config_name, run_id, timestamp) GROUP BY container'

/** The containers the `.vast` declares, and the cpu it reserved for each.
 *
 *  From `config_view` (the campaign's own config as `json_tree` rows) rather than
 *  `runs.available_cpus`: the declared value is literally the line the user will edit, whereas
 *  `available_cpus` is the Kubernetes downward API's report of it, which rounds a fractional
 *  request UP to the next whole core.
 *
 *  **Two kinds of row, and the first matters most when there is no second.** A `.vast` need not
 *  declare `resources` at all — and that campaign's author is the one who most needs this panel,
 *  since it is the only place telling them what to set. But the measured main container is
 *  recorded under the fixed role name `robovast`, so with no cpu rows to pair against there is
 *  nothing to translate it into a name that appears in their file. The container KEYS are in the
 *  config either way, so they are fetched too: the bare `$.execution.containers.<name>` object
 *  rows, isolated by excluding anything with a further dot (LIKE's `%` spans dots, so
 *  `NOT LIKE '….%.%'` is what makes the first pattern mean "one segment deep").
 *
 *  Not `parent = '$.execution.containers'`, which reads like the obvious filter and matches
 *  nothing: `config_view.parent` is `json_tree`'s integer node id, not a path. */
export const DETAILS_DECLARED_CPU_SQL =
  "SELECT fullkey, value FROM config_view WHERE " +
  "(fullkey LIKE '$.execution.containers.%' AND fullkey NOT LIKE '$.execution.containers.%.%') " +
  "OR fullkey LIKE '$.execution.containers.%.resources.cpu' " +
  "OR fullkey LIKE '$.execution.containers.%.resources.memory'"

/** Which scenario action each run ended in, cross-tabulated with the run's verdict.
 *
 *  Campaign- and SUT-independent by construction: it names no action, no topic and no plugin, and
 *  reads only the behaviour-tree columns every `behaviors`-schema table has. scenario_execution
 *  writes that table for every campaign (`--bt-log` is on by default), so the same query answers
 *  "what made the trial fail?" for a nav2 sweep and for a quadrotor landing alike.
 *
 *  **A leaf is an action.** Composites (`Sequence`, `Parallel`, decorators) go RUNNING too, and
 *  the root is RUNNING for the whole trial — attributing to those would answer "was the scenario
 *  running?" for every run. Leaf-ness is derived from `parent_id` rather than from the `type`
 *  column, because `type` is one of the LATER columns (a store postprocessed before it existed,
 *  or a table produced by another route, has only the original seven).
 *
 *  **The tip, not the last status change.** Ordering by time alone answers `emit end` /
 *  `emit fail` on every run — the scenario's own terminal marker, which is true and useless. Only
 *  actions that actually execute report RUNNING, so the last RUNNING leaf is the action the tree
 *  was working on when the trial ended: the goal that timed out, the assertion that tripped. An
 *  explicit FAILURE outranks it where one exists, since that is the attribution stated rather
 *  than inferred. */
export const DETAILS_ACTIONS_SQL =
  "SELECT r.status AS verdict, t.action, t.ended_as, COUNT(*) AS runs FROM (" +
  "SELECT config_name, run_id, behavior_name AS action, status_name AS ended_as, " +
  "ROW_NUMBER() OVER (PARTITION BY config_name, run_id " +
  "ORDER BY (status_name = 'FAILURE') DESC, timestamp DESC) AS rn " +
  "FROM behaviors WHERE status_name IN ('RUNNING', 'FAILURE') " +
  "AND behavior_id NOT IN (SELECT parent_id FROM behaviors WHERE parent_id IS NOT NULL)) t " +
  "JOIN run_view r ON r.config_name = t.config_name AND r.run_id = t.run_id " +
  "WHERE t.rn = 1 GROUP BY 1, 2, 3"

/** A campaign's runs can outnumber the client's default cap; this is the server's own cap. */
export const DETAILS_MAX_ROWS = 5000

// ---------------------------------------------------------------------------------------------
// Row shapes (what the three queries return)
// ---------------------------------------------------------------------------------------------

export interface RunRow {
  config_name?: string | null
  run_id?: number | null
  batch?: number | null
  status?: string | null
  duration_s?: number | null
  start_time?: string | null
  objective?: number | null
}

export interface CpuRow {
  container?: string | null
  p05?: number | null
  p25?: number | null
  p50?: number | null
  p75?: number | null
  p95?: number | null
  peak?: number | null
  core_seconds?: number | null
  ticks?: number | null
  /** Summed RSS per tick, same five percentiles plus the peak. */
  m05?: number | null
  m25?: number | null
  m50?: number | null
  m75?: number | null
  m95?: number | null
  m_peak?: number | null
  byte_seconds?: number | null
}

export interface DeclaredCpuRow {
  fullkey?: string | null
  value?: unknown
}

// ---------------------------------------------------------------------------------------------
// CPU sizing
// ---------------------------------------------------------------------------------------------

/** Headroom over sustained use. Absorbs the p95→peak gap; a brief excursion above the
 *  reservation costs CFS throttling for that scheduling period, which is the right price for
 *  not reserving the peak permanently. */
export const CPU_HEADROOM = 1.25

/** Reservations are rounded to quarter cores, not to raw floats: 4.75 is legible in a diff and
 *  reproduces exactly, while 4.7531… invites the reader to believe the measurement resolved
 *  something it did not. */
export const CPU_GRANULARITY = 0.25

/** Fewest 1 Hz samples a container needs before its p95 means anything.
 *
 *  Not defensive rounding: a campaign of sub-second runs (the quadrotor examples) yields single
 *  digits of in-window ticks across every run put together, and a p95 over seven points is just
 *  the maximum wearing a percentile's name. It produced a confident "0.5 cpu, 192 jobs in
 *  flight" from 7 samples. Below this, the measurement is still shown and the suggestion is
 *  withheld. */
export const CPU_MIN_TICKS = 30

/** When peak counts as a burst rather than ordinary variation.
 *
 *  Calibrated against real campaigns, where peak/p95 runs 1.3–2.5. The obvious test —
 *  `peak > suggested` — is true for almost any workload once the reservation is p95 × 1.25, so
 *  it flagged every container on every campaign and told the reader nothing. */
export const CPU_BURST_RATIO = 2

/** Headroom over the memory PEAK, and it is the peak deliberately.
 *
 *  CPU is sized on sustained use because exceeding a cpu reservation costs throttling for one
 *  scheduling period — slower, still correct. Exceeding a memory limit is an OOM kill: the run dies
 *  and the campaign loses a cell. The two failures are not comparable, so neither are their rules,
 *  and sizing memory on a percentile would be sizing it on how often the run survives. */
export const MEM_HEADROOM = 1.25

/** Memory reservations are rounded up to 128 MiB. Kubernetes accepts a byte count, but nobody
 *  writes one: the value goes into a `.vast` as `2Gi`, and a suggestion of `1876Mi` implies a
 *  measurement resolved to the megabyte when the numbers behind it move by more than that between
 *  runs. */
export const MEM_GRANULARITY_BYTES = 128 * 1024 * 1024

/** Round bytes up to the next `MEM_GRANULARITY_BYTES`, at least one unit. */
export function ceilToMemUnit(bytes: number): number {
  if (!Number.isFinite(bytes) || bytes <= 0) return MEM_GRANULARITY_BYTES
  return Math.max(
    MEM_GRANULARITY_BYTES,
    Math.ceil(bytes / MEM_GRANULARITY_BYTES) * MEM_GRANULARITY_BYTES,
  )
}

/** Bytes as the suffixed form a `.vast` is written in (`2Gi`, `512Mi`).
 *
 *  Not `format.ts`'s `formatBytes`, which is for reading (one decimal, "1.8 GiB"): every number
 *  here is meant to be typed into a config, so it has to be a value Kubernetes parses back to
 *  exactly what was computed. Suggestions are whole multiples of 128 MiB, so this is always exact. */
export function formatMemQuantity(bytes: number): string {
  // Whole gibibytes as Gi, everything else as Mi. Not a fractional Gi: `0.5Gi` parses, but nobody
  // writes it, and every suggestion is a whole multiple of 128Mi so Mi is always exact.
  if (bytes > 0 && bytes % 1024 ** 3 === 0) return `${bytes / 1024 ** 3}Gi`
  return `${Math.round(bytes / 1024 ** 2)}Mi`
}

/** CPU cores at the granularity they are actually reserved at.
 *
 *  Not `format.ts`'s `formatCores`, which is fixed at one decimal: it renders a suggested 0.75
 *  as "0.8" and a pod total of 4.25 as "4.3". Every number here is meant to be typed into a
 *  `.vast`, so displaying a value that differs from the one computed is the one thing this must
 *  not do. */
export function formatCpu(cores: number): string {
  return String(Math.round(cores * 100) / 100)
}

/** Round up to the next quarter core.
 *
 *  No float-error guard, deliberately: 0.25 is a power of two, so `cores / 0.25` is an exponent
 *  shift and therefore exact for every finite input. A value already on a quarter can never
 *  ceil up to the next one, which is the failure this would otherwise need protecting from. */
export function ceilToQuarter(cores: number): number {
  if (!Number.isFinite(cores) || cores <= 0) return CPU_GRANULARITY
  return Math.max(CPU_GRANULARITY, Math.ceil(cores / CPU_GRANULARITY) * CPU_GRANULARITY)
}

/** One resource's distribution for one container, plus what was reserved and what to reserve.
 *
 *  Shared by cpu and memory so the panel draws them with one component: the two differ in their
 *  unit and in how `suggested` is derived, not in what a reader does with them. */
export interface ResourceStats {
  p05: number
  p25: number
  p50: number
  p75: number
  p95: number
  peak: number
  /** Time-weighted MEAN over every tick — what the container actually used on average, which is
   *  the honest basis for "how much of the total was mine". The median would answer a different
   *  question (the typical tick) and the shares would not add up to what was consumed. */
  mean: number
  /** What the `.vast` declares, or null when it declares nothing for this container. */
  declared: number | null
  /** What to reserve, or null when there were too few samples to stand behind a number. */
  suggested: number | null
}

export interface ContainerCpu {
  /** The name the `.vast` declares, where it could be resolved — that is the line the user
   *  edits. Falls back to the measured name when there is no declaration to pair with. */
  container: string
  /** The spread of per-tick demand, for the box the panel draws. p05/p95 are the whiskers and
   *  p25/p75 the box; the median says where the container actually SITS, which the sizing
   *  percentile alone does not — a container idling at 0.2 with one brief 3-core burst and one
   *  holding 3 cores throughout share a p95 and want different answers. */
  p05: number
  p25: number
  p50: number
  p75: number
  /** Sustained use — the sizing basis. */
  p95: number
  /** Highest single tick. Shown, never sized on: it is what the headroom is for. */
  peak: number
  /** What the `.vast` declares, or null when it declares nothing for this container. */
  declared: number | null
  /** `ceilToQuarter(p95 * CPU_HEADROOM)`, or null when there were too few samples to judge. */
  suggested: number | null
  /** How many 1 Hz ticks the percentile was taken over. */
  ticks: number
  /** The same shape for memory, in BYTES. `suggested` here is peak-based (see `MEM_HEADROOM`), and
   *  every figure is an upper bound: `memory_rss_bytes` is summed across the processes sharing a
   *  name, so pages shared between a process and its forks are counted more than once. Read as a
   *  trend, and reserve above it rather than at it. */
  mem: ResourceStats
  /** Time-weighted mean cores — the share the ring is drawn from. */
  meanCores: number
  /** Core-seconds this container consumed. Ticks are 1 Hz, so the summed cores ARE core-seconds.
   *  Kept per container, not only in the campaign total, because "which container burned the
   *  campaign's CPU" is a different question from "how much should each reserve" — a container can
   *  be modest per tick and still dominate the bill by running throughout. */
  coreSeconds: number
  /** Fewer than `CPU_MIN_TICKS` samples: the measurement is shown, the suggestion withheld. */
  lowSample: boolean
  /** peak is at least `CPU_BURST_RATIO`× sustained — the reservation will throttle its bursts.
   *  Widening the headroom to cover them would mean reserving the peak permanently, so this is
   *  surfaced rather than absorbed. */
  bursty: boolean
}

export interface CpuSummary {
  containers: ContainerCpu[]
  /** Summed across containers, because a Kubernetes pod's request is the sum of its
   *  containers' and that is the figure that divides into the Kueue quota. Either is null
   *  unless every container contributed — a partial sum is not comparable to a whole one. */
  declaredPod: number | null
  suggestedPod: number | null
  /** The same two sums for memory, in bytes. */
  declaredMemPod: number | null
  suggestedMemPod: number | null
  /** What the campaign actually consumed. Ticks are 1 Hz, so summed cores are core-seconds. */
  cpuHours: number
}

/** Parse `$.execution.containers.<name>.resources.<field>` → `<name>`. */
function containerOfKey(fullkey: string, field: 'cpu' | 'memory'): string | null {
  const m = new RegExp(`^\\$\\.execution\\.containers\\.([^.]+)\\.resources\\.${field}$`).exec(
    fullkey,
  )
  return m ? m[1] : null
}

/** Parse the bare `$.execution.containers.<name>` row → `<name>`. */
function containerOfDeclarationKey(fullkey: string): string | null {
  const m = /^\$\.execution\.containers\.([^.]+)$/.exec(fullkey)
  return m ? m[1] : null
}

/** What `resource_usage` calls the main container, regardless of what the `.vast` named it.
 *  Mirrors `common/log_tail.MAIN_CONTAINER`. */
export const MEASURED_MAIN_CONTAINER = 'robovast'

/** Pair each measured container with the cpu its `.vast` declared.
 *
 *  Not a plain name join: `resource_usage` records the MAIN container under the fixed role name
 *  `robovast` while the campaign declares it by its own name (`scenario`, in every campaign on
 *  this machine) — see `expected_container_files` in results_processing/resource_usage.py, which
 *  names the main file for its ROLE and every sidecar for its container. Secondaries do match
 *  exactly.
 *
 *  So: match by name first, then pair the measured `robovast` row with whatever single
 *  declaration went unclaimed. If the leftovers are ambiguous (a declared container that never
 *  produced samples, so more than one is unmatched), the main container reports no declaration
 *  rather than guessing — a wrong pairing would compare one container's use against another's
 *  reservation, which is worse than saying nothing.
 *
 *  Returned keyed by the DECLARED name where one is known, because that is the line the user
 *  will edit in the `.vast`; `robovast` is an internal role name they cannot act on. */
function resolveDeclared(
  measured: string[],
  declared: Map<string, number>,
  declaredMem: Map<string, number> = new Map(),
  names: Set<string> = new Set(declared.keys()),
): Map<string, { label: string; cpu: number | null; memory: number | null }> {
  const out = new Map<string, { label: string; cpu: number | null; memory: number | null }>()
  // Pairing is done over the container NAMES, which the campaign always declares, rather than over
  // the cpu map, which it need not: a `.vast` with no `resources` block at all must still relabel
  // `robovast` to the container it actually is, because naming a container the reader cannot find
  // in their file is worst precisely where the recommendation is the only thing on offer.
  const unclaimed = new Set(names)
  const entry = (label: string) => ({
    label,
    cpu: declared.get(label) ?? null,
    memory: declaredMem.get(label) ?? null,
  })
  for (const name of measured) {
    if (unclaimed.has(name)) {
      unclaimed.delete(name)
      out.set(name, entry(name))
    }
  }
  for (const name of measured) {
    if (out.has(name)) continue
    if (name === MEASURED_MAIN_CONTAINER && unclaimed.size === 1) {
      const [label] = [...unclaimed]
      out.set(name, entry(label))
    } else {
      out.set(name, { label: name, cpu: null, memory: null })
    }
  }
  return out
}

/** Kubernetes CPU quantities: a number, or a millicore string like "500m". Anything else is not
 *  a declaration this panel can compare against, so it reads as absent rather than as zero.
 *
 *  Mirrors `robovast.common.quantity.to_cores`, which is the authority — the config layer rejects
 *  what that function cannot read, so anything reaching here has already passed it. */
function parseCpuQuantity(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'string') return null
  const text = value.trim()
  if (/^\d+(\.\d+)?m$/.test(text)) return parseFloat(text.slice(0, -1)) / 1000
  const n = Number(text)
  return Number.isFinite(n) ? n : null
}

/** Kubernetes memory quantities → bytes. Mirrors `robovast.common.quantity.to_bytes`.
 *
 *  Binary suffixes are the ones that matter (`2Gi`), but the decimal ones are legal Kubernetes and
 *  differ by 7% at gigabyte scale, so they are read as what they mean rather than treated as
 *  aliases. */
function parseMemQuantity(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) && value >= 0 ? value : null
  if (typeof value !== 'string') return null
  const match = /^(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|Ti|K|M|G|T|)$/.exec(value.trim())
  if (!match) return null
  const multiplier: Record<string, number> = {
    '': 1,
    K: 1e3, M: 1e6, G: 1e9, T: 1e12,
    Ki: 1024, Mi: 1024 ** 2, Gi: 1024 ** 3, Ti: 1024 ** 4,
  }
  return parseFloat(match[1]) * multiplier[match[2]]
}

/** Fold the CPU and declared-cpu rows into what the panel shows.
 *
 *  Returns null when there are no CPU rows at all — a campaign that was never postprocessed has
 *  no `resource_usage` table, and "no measurement" must not render as "used nothing". */
export function summariseCpu(
  cpuRows: CpuRow[],
  declaredRows: DeclaredCpuRow[] = [],
): CpuSummary | null {
  const declared = new Map<string, number>()
  const declaredMem = new Map<string, number>()
  const names = new Set<string>()
  for (const row of declaredRows) {
    if (typeof row.fullkey !== 'string') continue
    const declaration = containerOfDeclarationKey(row.fullkey)
    if (declaration) {
      names.add(declaration)
      continue
    }
    const key = containerOfKey(row.fullkey, 'cpu')
    if (key) {
      // A container that declares a cpu is a container, whether or not this row's value parsed.
      names.add(key)
      const cpu = parseCpuQuantity(row.value)
      if (cpu !== null) declared.set(key, cpu)
      continue
    }
    const memKey = containerOfKey(row.fullkey, 'memory')
    if (memKey) {
      names.add(memKey)
      const bytes = parseMemQuantity(row.value)
      if (bytes !== null) declaredMem.set(memKey, bytes)
    }
  }

  const usable = cpuRows.filter(
    (row) =>
      !!row.container &&
      Number.isFinite(Number(row.p95 ?? 0)) &&
      Number.isFinite(Number(row.peak ?? 0)),
  )
  const resolved = resolveDeclared(
    usable.map((row) => row.container as string),
    declared,
    declaredMem,
    names,
  )

  const containers: ContainerCpu[] = []
  let coreSeconds = 0
  for (const row of usable) {
    const p95 = Number(row.p95 ?? 0)
    const peak = Number(row.peak ?? 0)
    const ticks = Number(row.ticks ?? 0)
    const lowSample = ticks < CPU_MIN_TICKS
    const match = resolved.get(row.container as string)
    // A store written before the percentiles were queried has only p95; the box then collapses
    // onto it rather than reading as a container that never varied.
    const quantile = (v: unknown, fallback: number) =>
      Number.isFinite(Number(v ?? NaN)) ? Number(v) : fallback
    const containerCoreSeconds = Number(row.core_seconds ?? 0)
    const memPeak = quantile(row.m_peak, 0)
    const byteSeconds = Number(row.byte_seconds ?? 0)
    containers.push({
      container: match?.label ?? (row.container as string),
      p05: quantile(row.p05, p95),
      p25: quantile(row.p25, p95),
      p50: quantile(row.p50, p95),
      p75: quantile(row.p75, p95),
      p95,
      peak,
      declared: match?.cpu ?? null,
      suggested: lowSample ? null : ceilToQuarter(p95 * CPU_HEADROOM),
      mem: {
        p05: quantile(row.m05, memPeak),
        p25: quantile(row.m25, memPeak),
        p50: quantile(row.m50, memPeak),
        p75: quantile(row.m75, memPeak),
        p95: quantile(row.m95, memPeak),
        peak: memPeak,
        mean: ticks > 0 ? byteSeconds / ticks : 0,
        declared: match?.memory ?? null,
        // Sized on the PEAK, not a percentile: exceeding a memory limit is an OOM kill, not
        // throttling. Withheld on a thin sample for the same reason cpu's is.
        suggested: lowSample || memPeak <= 0 ? null : ceilToMemUnit(memPeak * MEM_HEADROOM),
      },
      ticks,
      lowSample,
      bursty: p95 > 0 && peak >= p95 * CPU_BURST_RATIO,
      coreSeconds: containerCoreSeconds,
      meanCores: ticks > 0 ? containerCoreSeconds / ticks : 0,
    })
    coreSeconds += containerCoreSeconds
  }
  if (!containers.length) return null

  containers.sort(
    (a, b) => (b.suggested ?? b.p95) - (a.suggested ?? a.p95) || a.container.localeCompare(b.container),
  )

  // Both pod totals are all-or-nothing. A sum missing one container is a different quantity from
  // the one it would be compared against, and showing it beside a complete total invites exactly
  // the comparison that is wrong.
  const complete = <T,>(pick: (c: ContainerCpu) => T | null): T[] | null => {
    const values = containers.map(pick)
    return values.some((v) => v === null) ? null : (values as T[])
  }
  const sum = (values: number[] | null) => (values ? values.reduce((n, v) => n + v, 0) : null)
  return {
    containers,
    declaredPod: sum(complete((c) => c.declared)),
    suggestedPod: sum(complete((c) => c.suggested)),
    declaredMemPod: sum(complete((c) => c.mem.declared)),
    suggestedMemPod: sum(complete((c) => c.mem.suggested)),
    cpuHours: coreSeconds / 3600,
  }
}

/** How many of these pods fit in a CPU quota.
 *
 *  An ESTIMATE, and labelled one wherever it is shown. It assumes the whole quota is free and
 *  that packing is perfect; neither holds. A pod is atomic — 9 cpu with 6 free waits however
 *  much total quota exists — and the quota is shared with every other campaign. The reliable
 *  half of the claim is qualitative: a smaller request fits more often, which matters most
 *  exactly when the cluster is busy. */
export function jobsInFlight(podCpu: number | null, quotaCpu: number | null): number | null {
  if (!podCpu || !quotaCpu || podCpu <= 0 || quotaCpu <= 0) return null
  return Math.floor(quotaCpu / podCpu)
}

// ---------------------------------------------------------------------------------------------
// Runs: batches and durations
// ---------------------------------------------------------------------------------------------

const PASSED = 'passed'
const FAILED = new Set(['failed', 'error'])

export interface BatchSummary {
  /** null for the campaign-wide row. */
  batch: number | null
  runs: number
  passed: number
  failed: number
  /** Neither passed nor failed: no verdict reached us (a missing `test.xml`), or the draw never
   *  composed. Counted apart so the three tallies always add up to `runs`. */
  other: number
  /** Median of the runs that recorded one; null when none did. */
  medianDuration: number | null
  /** Summed run durations for this row — the campaign's, or one batch's. */
  simulatedSeconds: number
  /** By the campaign's declared direction; null when the campaign has no scalar objective. */
  bestObjective: number | null
}

function median(values: number[]): number | null {
  if (!values.length) return null
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

function summariseRows(rows: RunRow[], batch: number | null, maximize: boolean): BatchSummary {
  let passed = 0
  let failed = 0
  const durations: number[] = []
  let best: number | null = null
  for (const row of rows) {
    if (row.status === PASSED) passed += 1
    else if (row.status && FAILED.has(row.status)) failed += 1
    if (typeof row.duration_s === 'number' && Number.isFinite(row.duration_s)) {
      durations.push(row.duration_s)
    }
    if (typeof row.objective === 'number' && Number.isFinite(row.objective)) {
      if (best === null) best = row.objective
      else best = maximize ? Math.max(best, row.objective) : Math.min(best, row.objective)
    }
  }
  return {
    batch,
    runs: rows.length,
    passed,
    failed,
    other: rows.length - passed - failed,
    medianDuration: median(durations),
    simulatedSeconds: durations.reduce((n, v) => n + v, 0),
    bestObjective: best,
  }
}

/** The overview row (`batch: null`) first, then one row per batch.
 *
 *  Every campaign gets the overview row. A batch-mode campaign has exactly one batch, so its
 *  per-batch row would repeat the overview verbatim — it is omitted, and such a campaign shows
 *  one row. A search campaign's rounds are the thing worth reading (each is an ask/tell round
 *  the strategy proposed), so those become a row each beneath the overview. */
export function summariseBatches(rows: RunRow[], maximize = true): BatchSummary[] {
  const byBatch = new Map<number, RunRow[]>()
  for (const row of rows) {
    if (typeof row.batch !== 'number') continue
    const list = byBatch.get(row.batch)
    if (list) list.push(row)
    else byBatch.set(row.batch, [row])
  }
  const all = summariseRows(rows, null, maximize)
  if (byBatch.size < 2) return [all]
  const perBatch = [...byBatch.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([idx, batchRows]) => summariseRows(batchRows, idx, maximize))
  return [all, ...perBatch]
}

export interface DurationBin {
  /** Bin bounds in seconds. `from` is inclusive, `to` exclusive — except the last bin, which
   *  includes its upper bound so the slowest run lands somewhere. */
  from: number
  to: number
  runs: number
  passed: number
  failed: number
  other: number
}

/** Bins across a ~190px column, at 1px between them — so each is about 7px wide.
 *
 *  Narrow on purpose: a wide bar smooths a bimodal spread into one hump, and telling a second mode
 *  from a long tail is most of what this chart is for. The floor is the 2px minimum width in
 *  `DurationHistogram`; below that neighbouring bins stop being separable. */
export const HISTOGRAM_BINS = 24

/** Narrowest range the axis will show, in seconds.
 *
 *  Without a floor the axis rescales to whatever spread the data happens to have, so a campaign
 *  whose runs agreed to within 40 ms draws the same broad distribution as one spanning ten minutes
 *  — the same picture for "identical" and for "wildly variable", which is the one confusion a
 *  histogram must not create. Ten seconds is the resolution below which run-to-run variation is
 *  scheduling noise rather than behaviour. */
export const HISTOGRAM_MIN_SPAN = 10

/** The distribution of run durations.
 *
 *  Binned over the DATA's range rather than from zero. A campaign of six runs that all took about
 *  100 s would otherwise be one bar against eleven empty ones — technically honest, and it shows
 *  nothing. Both ends are labelled where it is drawn, so the range is stated rather than assumed.
 *
 *  Each bin is split by verdict, because the useful shape is rarely just "how long": a second mode
 *  made entirely of failures is a timeout, and the same mode made of passes is a slower path
 *  through the scenario. One histogram carries both readings; a plain count carries neither. */
export function durationHistogram(rows: RunRow[], bins = HISTOGRAM_BINS): DurationBin[] {
  const durations = rows
    .filter(
      (row) =>
        typeof row.duration_s === 'number' && Number.isFinite(row.duration_s) && row.duration_s > 0,
    )
    .map((row) => ({ duration: row.duration_s as number, status: row.status }))
  if (!durations.length) return []

  const values = durations.map((d) => d.duration)
  const observed = { min: Math.min(...values), max: Math.max(...values) }
  // The axis spans at least HISTOGRAM_MIN_SPAN, centred on the data. Two reasons, and the second is
  // the important one: a campaign whose runs all took the same length has a zero span and nothing to
  // divide by, AND a campaign whose runs varied by 40ms would otherwise be spread across the full
  // width as if that were a distribution — twenty-four bins of noise, drawn exactly like a real
  // spread. A fixed floor makes "they were all the same" LOOK like one spike, which is what it is.
  const span = Math.max(observed.max - observed.min, HISTOGRAM_MIN_SPAN)
  const centre = (observed.min + observed.max) / 2
  // Clamped at zero: a negative duration is not a thing, and an axis starting below it wastes a
  // quarter of the width on impossible values.
  const min = Math.max(0, centre - span / 2)
  const width = span / bins

  const out: DurationBin[] = Array.from({ length: bins }, (_, i) => ({
    from: min + i * width,
    to: min + (i + 1) * width,
    runs: 0,
    passed: 0,
    failed: 0,
    other: 0,
  }))
  for (const { duration, status } of durations) {
    // Clamped at both ends: the top bin owns its upper bound (without this the slowest run indexes
    // one past the end), and the bottom one absorbs anything the zero clamp pushed left of `min`.
    const index = Math.min(bins - 1, Math.max(0, Math.floor((duration - min) / width)))
    const bin = out[index]
    bin.runs += 1
    if (status === PASSED) bin.passed += 1
    else if (status && FAILED.has(status)) bin.failed += 1
    else bin.other += 1
  }
  return out
}

// ---------------------------------------------------------------------------------------------
// Actions: what the trial ended in
// ---------------------------------------------------------------------------------------------

export interface ActionRow {
  verdict?: string | null
  action?: string | null
  ended_as?: string | null
  runs?: number | null
}

/** One bar segment: an action, a verdict, and how many runs ended there. */
export interface ActionPoint {
  action: string
  /** `passed` / `failed` / `other`, folded from the run's status the same way the batch rows
   *  fold it, so the two never disagree about what "failed" counts. */
  verdict: string
  runs: number
}

/** How many actions the panel shows. Five, because the column is ~110px tall and a sixth bar is
 *  thinner than its own label; a campaign whose runs end in more than five different places is
 *  telling you something the top five already carry. */
export const TOP_ACTIONS = 5

/** The actions the most runs ended in, as stacked segments per verdict.
 *
 *  Ranked by TOTAL runs rather than by failures alone: an action that ends 20 passing runs and 5
 *  failing ones is the campaign's main path with a defect, while one that ends 5 failing runs and
 *  nothing else is a wall — and only the two segments side by side distinguish them. Ranking by
 *  failures would show the second and hide the first. */
export function summariseActions(rows: ActionRow[]): ActionPoint[] {
  const byAction = new Map<string, Map<string, number>>()
  const totals = new Map<string, number>()
  for (const row of rows) {
    if (typeof row.action !== 'string' || !row.action) continue
    const runs = Number(row.runs ?? 0)
    if (!Number.isFinite(runs) || runs <= 0) continue
    const verdict =
      row.verdict === PASSED ? 'passed' : row.verdict && FAILED.has(row.verdict) ? 'failed' : 'other'
    const perVerdict = byAction.get(row.action) ?? new Map<string, number>()
    perVerdict.set(verdict, (perVerdict.get(verdict) ?? 0) + runs)
    byAction.set(row.action, perVerdict)
    totals.set(row.action, (totals.get(row.action) ?? 0) + runs)
  }
  const ranked = [...totals.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, TOP_ACTIONS)
  const points: ActionPoint[] = []
  for (const [action] of ranked) {
    // Emitted failed-first so the stack reads outward from the axis with the failures against it,
    // which is the comparison the column is for.
    for (const verdict of ['failed', 'other', 'passed']) {
      const runs = byAction.get(action)?.get(verdict)
      if (runs) points.push({ action, verdict, runs })
    }
  }
  return points
}

// ---------------------------------------------------------------------------------------------
// Runs worth a second look
// ---------------------------------------------------------------------------------------------

export interface OutlierRun {
  /** `<config>/<run>` — the address that finds it in the Results explorer. */
  run: string
  config: string
  duration_s: number
  /** What the rest of this configuration took. */
  median: number
  /** `duration_s / median`. >1 is slower than its siblings, <1 faster. */
  ratio: number
  status: string
}

/** How far from its siblings a run has to be before it is worth naming. 1.5x is a run that took
 *  half again as long as the rest of its cell — comfortably past ordinary scheduling jitter on a
 *  shared cluster, and small enough to catch a goal that needed one recovery. */
export const OUTLIER_RATIO = 1.5

/** Fewest sibling runs before "the others" means anything. Two runs have no majority to be an
 *  outlier FROM: the median is their mean and each is equally far from it. */
export const OUTLIER_MIN_SIBLINGS = 3

/** How many outliers the panel names. */
export const OUTLIER_LIMIT = 5

/** Runs that stand out from the OTHER RUNS OF THE SAME CONFIGURATION.
 *
 *  Within a configuration, and never across the campaign: duration is dominated by which cell a
 *  run belongs to — a goal across the depot legitimately takes several times one beside the robot
 *  — so a campaign-wide comparison ranks the configurations and calls the slowest cell's every
 *  run an outlier. Compared against its own siblings, the same run is unremarkable and a genuine
 *  straggler in the fast cell becomes visible.
 *
 *  Ranked by how far from 1 the ratio is, so a run that finished suspiciously EARLY (a trial that
 *  gave up in seconds) ranks alongside one that dragged. Both are the same question: why did this
 *  run not do what its siblings did? */
export function outlierRuns(rows: RunRow[], limit = OUTLIER_LIMIT): OutlierRun[] {
  const byConfig = new Map<string, RunRow[]>()
  for (const row of rows) {
    if (typeof row.duration_s !== 'number' || !Number.isFinite(row.duration_s)) continue
    if (row.duration_s <= 0) continue
    const config = typeof row.config_name === 'string' ? row.config_name : '?'
    const list = byConfig.get(config)
    if (list) list.push(row)
    else byConfig.set(config, [row])
  }

  const found: OutlierRun[] = []
  for (const [config, runs] of byConfig) {
    if (runs.length < OUTLIER_MIN_SIBLINGS) continue
    const mid = median(runs.map((r) => r.duration_s as number))
    if (mid === null || mid <= 0) continue
    for (const row of runs) {
      const duration = row.duration_s as number
      const ratio = duration / mid
      if (ratio < OUTLIER_RATIO && ratio > 1 / OUTLIER_RATIO) continue
      found.push({
        run: `${config}/${row.run_id ?? '?'}`,
        config,
        duration_s: duration,
        median: mid,
        ratio,
        status: typeof row.status === 'string' ? row.status : 'unknown',
      })
    }
  }
  return found
    .sort((a, b) => Math.abs(Math.log(b.ratio)) - Math.abs(Math.log(a.ratio)))
    .slice(0, limit)
}

// ---------------------------------------------------------------------------------------------
// The numbers strip
// ---------------------------------------------------------------------------------------------

export interface DetailsTotals {
  runs: number
  batches: number
  /** Summed run durations. Reported as *simulated* time because the simulator is launched with
   *  `--pacing realtime`, so one simulated second is one wall second by construction. If pacing
   *  ever becomes configurable this needs the real clock-map span behind it. */
  simulatedSeconds: number
  /** null when the campaign was never postprocessed. */
  cpuHours: number | null
  /** Wall-clock from the first run starting to the last one finishing; null when the runs carry
   *  no usable start times. Not the sum of durations — runs overlap, and that is the point. */
  wallSeconds: number | null
  /** Throughput: completed runs per minute of that wall clock.
   *
   *  The number that says whether the sweep is worth running wider, and the one a smaller CPU
   *  reservation moves — halve the pod and twice as many fit the quota. Measured in RUNS, which is
   *  what the data records; a job with `execution.runs_per_job > 1` carries several of them, so on
   *  a packed campaign this is above the rate at which jobs are dispatched. */
  runsPerMinute: number | null
}

/** First start to last finish, in seconds. Null when no run recorded a usable start. */
function wallSpanSeconds(rows: RunRow[]): number | null {
  let first = Infinity
  let last = -Infinity
  for (const row of rows) {
    if (typeof row.start_time !== 'string') continue
    const start = Date.parse(row.start_time) / 1000
    if (!Number.isFinite(start)) continue
    const duration = typeof row.duration_s === 'number' && Number.isFinite(row.duration_s)
      ? Math.max(0, row.duration_s)
      : 0
    first = Math.min(first, start)
    last = Math.max(last, start + duration)
  }
  if (!Number.isFinite(first) || !Number.isFinite(last)) return null
  return Math.max(0, last - first)
}

export function totals(rows: RunRow[], cpu: CpuSummary | null): DetailsTotals {
  let simulatedSeconds = 0
  const batches = new Set<number>()
  for (const row of rows) {
    if (typeof row.duration_s === 'number' && Number.isFinite(row.duration_s)) {
      simulatedSeconds += row.duration_s
    }
    if (typeof row.batch === 'number') batches.add(row.batch)
  }
  const wallSeconds = wallSpanSeconds(rows)
  return {
    runs: rows.length,
    batches: batches.size,
    simulatedSeconds,
    cpuHours: cpu ? cpu.cpuHours : null,
    wallSeconds,
    // A campaign whose runs all share one instant has no rate to report -- dividing by zero would
    // print Infinity where "not enough to say" is the truth.
    runsPerMinute: wallSeconds && wallSeconds > 0 ? rows.length / (wallSeconds / 60) : null,
  }
}

/** "9 → 4.25 cpu", the headline shown while the panel is collapsed. Null when there is nothing
 *  to suggest — no measurement, or too few samples to stand behind one. */
export function cpuHeadline(cpu: CpuSummary | null): string | null {
  if (!cpu || cpu.suggestedPod === null) return null
  const suggested = `${formatCpu(cpu.suggestedPod)} cpu`
  if (cpu.declaredPod === null) return `${suggested} suggested`
  return `${formatCpu(cpu.declaredPod)} → ${suggested}`
}

/** The same for memory: "12Gi → 3Gi", or the suggestion alone when nothing was declared. */
export function memHeadline(cpu: CpuSummary | null): string | null {
  if (!cpu || cpu.suggestedMemPod === null) return null
  const suggested = formatMemQuantity(cpu.suggestedMemPod)
  if (cpu.declaredMemPod === null) return `${suggested} suggested`
  return `${formatMemQuantity(cpu.declaredMemPod)} → ${suggested}`
}
