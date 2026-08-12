// Client-side model for the Results Explorer tree (campaign → [batch →] config → run).
// Parallels `fileTree.ts`: pure, React-free helpers that turn the service's data into a tree
// the RichTreeView renders. Campaign-level status comes from `CampaignSummary`; the per-config
// / per-run breakdown is derived from rows of `run_view` (fetched via `queryCampaignDataSql`),
// since there is no dedicated per-run REST endpoint.
//
// The batch level appears only for a **search** campaign, where it is the unit of search
// history (one ask/tell round). A batch-mode campaign has exactly one batch, so grouping by it
// would say nothing, and its tree keeps the shape it always had.

import { isFailed, isRunning, type CampaignSummary } from './robovastClient'

// The tree's one query, shared by the Explorer and the Run view's picker (see `runsQuery`).
//
// `run_view`, not the postprocessed `runs` table: it is a temp view over the live `campaign.db`
// (written as the campaign runs), so a campaign that produced no rosbags -- and therefore has no
// `data.db` at all -- still lists its runs. It also means both surfaces build the same tree from
// the same rows; they used to read two different tables for it.
//
// `batch` is the ask/tell round that proposed the configuration, and `objective` its score --
// the two things that make a search's history readable. `objective_direction` comes from the
// campaign's own `.vast` because "best" is meaningless without it: taking the max of a
// minimised objective would report the worst draw of every round as its best. It is a scalar
// subselect rather than a second request: one row's worth of campaign metadata, on a query
// that is already per-campaign.
export const CAMPAIGN_RUNS_SQL =
  'SELECT config_name, run_id, status, passed, objective, batch, ' +
  "(SELECT json_extract(config_json, '$.search.objectives[0].direction') " +
  'FROM campaign.campaign LIMIT 1) AS objective_direction ' +
  'FROM run_view ORDER BY batch, config_name, run_id'

// The whole tree of one campaign comes from a single query, so the cap has to hold every run of
// every config. 500 (the client default) silently truncated a campaign of 200 runs with
// repetitions; this is the server's own cap.
export const CAMPAIGN_RUNS_MAX_ROWS = 5000

// Pass/fail status of any node, mapped to a theme color by `statusColor`. `neutral` = no verdict
// yet (e.g. a campaign that hasn't been postprocessed); `running` = still executing; `skipped` = a
// search parameter set whose configuration could not be built at all, so it never ran — distinct
// from `failed` (which ran and lost) and from `unknown` (which may yet have a verdict).
export type NodeStatus = 'passed' | 'failed' | 'skipped' | 'unknown' | 'running' | 'neutral'

export type NodeKind = 'campaign' | 'batch' | 'config' | 'run' | 'placeholder'

// One RichTreeView item. Extra fields beyond {id,label,children} are read back in the click
// handler and the custom item renderer via the tree's `publicAPI.getItem(itemId)`.
export interface ResultsTreeItem {
  id: string
  label: string
  status: NodeStatus
  kind: NodeKind
  campaignId: string
  configName?: string
  runId?: number
  /** The search round this node belongs to. Set on `batch` nodes, and on the config/run nodes
   *  beneath one — a batch has no directory, so this is what identifies it to the service. */
  batch?: number
  /** A config's objective score. null when the campaign records no scalar objective (batch
   *  mode, or a multi-objective search); used for the label and the batch's best. */
  objective?: number | null
  /** e.g. "5/5" (passed/total) shown as a trailing chip; omitted for leaves/placeholders. */
  count?: string
  disabled?: boolean
  children?: ResultsTreeItem[]
}

/** Which optimisation direction a campaign's objective is scored in. */
export type ObjectiveDirection = 'minimize' | 'maximize'

/** How to build a campaign's subtree: whether to group its configs by the round that proposed
 *  them, and which way "best" points. Both default to the ungrouped, direction-less case, so a
 *  caller that knows nothing about searches gets exactly the pre-batch tree. */
export interface CampaignTreeOptions {
  grouped?: boolean
  direction?: ObjectiveDirection
}

// Map a NodeStatus onto a MUI theme palette path (used with `sx` color lookups).
export function statusColor(status: NodeStatus): string {
  switch (status) {
    case 'passed':
      return 'success.main'
    case 'failed':
      return 'error.main'
    case 'running':
      return 'warning.main'
    // Deliberately not `error.main`: a draw that was never realizable is not a
    // regression to chase, and colouring it as one buries the runs that did fail.
    case 'skipped':
      return 'info.main'
    case 'unknown':
      return 'text.secondary'
    default:
      return 'text.disabled'
  }
}

// Campaign verdict from its summary counts + phase. A still-running campaign is `running`; a
// finished one is `failed` if the controller failed or any run failed, `passed` if postprocessed
// with no failures, otherwise `neutral` (nothing to judge yet).
export function campaignStatus(c: CampaignSummary): NodeStatus {
  if (isRunning(c)) return 'running'
  if (isFailed(c) || c.num_failed > 0) return 'failed'
  if (c.postprocessed && c.num_passed > 0 && c.num_failed === 0) return 'passed'
  return 'neutral'
}

// A single run's verdict, from the `run_view.status` column (passed | error | failed |
// composition_failed | unknown).
export function runStatus(row: Record<string, unknown>): NodeStatus {
  const s = String(row.status ?? '').toLowerCase()
  if (s === 'passed') return 'passed'
  if (s === 'failed' || s === 'error') return 'failed'
  if (s === 'composition_failed') return 'skipped'
  return 'unknown'
}

// An inner node's rollup from its children: any failure → failed; all passed → passed; all
// skipped → skipped (nothing ever became runnable, so there is nothing pending); else unknown.
// Used at both levels — for a config over its runs, and for a batch over its configs, where
// "all skipped" is a round whose every draw was unrealizable.
function rollupStatuses(children: NodeStatus[]): NodeStatus {
  if (children.some((s) => s === 'failed')) return 'failed'
  if (children.length > 0 && children.every((s) => s === 'passed')) return 'passed'
  if (children.length > 0 && children.every((s) => s === 'skipped')) return 'skipped'
  return 'unknown'
}

// The tree id of a run node — the one place a run's id is spelled, so the Explorer (which
// builds the tree) and the Run view (which reconstructs the id of the run it is showing, to
// highlight it) cannot drift. `batch` is null for an ungrouped campaign, which reproduces the
// pre-batch id exactly.
export function runNodeId(
  campaignId: string,
  batch: number | null,
  configName: string,
  runId: number | string,
): string {
  return `${configPrefix(campaignId, batch)}//cfg/${configName}//run/${runId}`
}

/** Where a config node hangs: off its batch when grouped, off the campaign when not. */
function configPrefix(campaignId: string, batch: number | null): string {
  return batch == null ? campaignId : `${campaignId}//batch/${batch}`
}

/** The optimisation direction the campaign's objective is scored in. Defaults to `maximize`,
 *  matching the `.vast` schema's own default for an objective that does not name one. */
export function objectiveDirection(rows: Record<string, unknown>[]): ObjectiveDirection {
  const raw = String(rows[0]?.objective_direction ?? '').toLowerCase()
  return raw === 'minimize' ? 'minimize' : 'maximize'
}

/** An objective as a label suffix — the desktop viewer's `%.4g`, which keeps a score readable
 *  without implying more precision than a search result has. */
function fmtObjective(value: number): string {
  const fixed = value.toPrecision(4)
  // toPrecision keeps trailing zeros ("0.8400") and can pick exponential form; Number()
  // normalises both, so 0.84 reads as "0.84" rather than "0.8400".
  return String(Number(fixed))
}

/** The objective of a config, or null when it has none (a multi-objective search records no
 *  scalar, and a batch-mode unit has no objective at all). */
function configObjective(rows: Record<string, unknown>[]): number | null {
  for (const row of rows) {
    if (row.objective !== null && row.objective !== undefined) return Number(row.objective)
  }
  return null
}

// The top-level label/status for a campaign node (children are attached lazily on expand).
export function campaignItem(c: CampaignSummary): ResultsTreeItem {
  const skipped = c.num_composition_failed ?? 0
  // Appended rather than folded into the denominator: those draws produced no runs, so
  // counting them there would understate the pass rate of the runs that did happen —
  // while dropping them entirely is what made a half-uncomposable search look complete.
  const runs = c.num_runs > 0 ? `${c.num_passed}/${c.num_runs}` : undefined
  const count = skipped > 0 ? [runs, `${skipped} skipped`].filter(Boolean).join(' · ') : runs
  return {
    id: c.campaign_id,
    label: c.campaign_id,
    kind: 'campaign',
    campaignId: c.campaign_id,
    status: campaignStatus(c),
    count,
  }
}

// Build one campaign's subtree from its `run_view` rows (already ordered by batch, config_name,
// run_id). Ungrouped this is campaign → config → run; grouped it is campaign → batch → config →
// run. The two shapes are not two algorithms: `configNodes` is the whole tree for an ungrouped
// campaign *and* the body of each batch, so an ungrouped campaign's node ids are what they
// always were by construction, rather than by a branch that has to remember to preserve them.
export function buildCampaignChildren(
  campaignId: string,
  rows: Record<string, unknown>[],
  opts: CampaignTreeOptions = {},
): ResultsTreeItem[] {
  const direction = opts.direction ?? 'maximize'
  if (!opts.grouped) return configNodes(campaignId, null, rows)

  // Rows whose batch was never recorded (an orphan `unit.batch_id`, or a store predating the
  // batch table) hang off the campaign beside the rounds. Inventing a round for them would
  // claim history the store does not have; dropping them would lose runs.
  const byBatch = new Map<number, Record<string, unknown>[]>()
  const unrecorded: Record<string, unknown>[] = []
  for (const row of rows) {
    if (row.batch === null || row.batch === undefined) {
      unrecorded.push(row)
      continue
    }
    const batch = Number(row.batch)
    const list = byBatch.get(batch)
    if (list) list.push(row)
    else byBatch.set(batch, [row])
  }

  const batches = [...byBatch.entries()]
    .sort(([a], [b]) => a - b)
    .map(([batch, batchRows]) => batchNode(campaignId, batch, batchRows, direction))
  return [...batches, ...configNodes(campaignId, null, unrecorded)]
}

// One search round: its configs, with the pass tally of the runs beneath them and the best
// objective it found.
function batchNode(
  campaignId: string,
  batch: number,
  rows: Record<string, unknown>[],
  direction: ObjectiveDirection,
): ResultsTreeItem {
  const configs = configNodes(campaignId, batch, rows)
  // Runs, not configs, so a batch's chip adds up to the campaign's own.
  const runs = rows.filter((r) => r.run_id !== null && r.run_id !== undefined)
  const passed = runs.filter((r) => runStatus(r) === 'passed').length
  const skipped = configs.filter((c) => c.status === 'skipped').length
  const count = [
    runs.length > 0 ? `${passed}/${runs.length}` : null,
    skipped > 0 ? `${skipped} skipped` : null,
  ].filter(Boolean).join(' · ')

  const objectives = configs
    .map((c) => c.objective)
    .filter((o): o is number => o !== null && o !== undefined)
  const best = objectives.length
    ? direction === 'minimize' ? Math.min(...objectives) : Math.max(...objectives)
    : null

  return {
    id: `${campaignId}//batch/${batch}`,
    label: best === null ? `batch ${batch}` : `batch ${batch}  best ${fmtObjective(best)}`,
    kind: 'batch',
    campaignId,
    batch,
    status: rollupStatuses(configs.map((c) => c.status)),
    count: count || undefined,
    children: configs,
  }
}

// The config → run nodes for a set of rows. `batch` is null for an ungrouped campaign and the
// round's index inside a batch node; it only shapes the ids and rides along on the items so the
// service can be told which batch a selection belongs to.
function configNodes(
  campaignId: string,
  batch: number | null,
  rows: Record<string, unknown>[],
): ResultsTreeItem[] {
  const prefix = configPrefix(campaignId, batch)
  // Two units of one round can share a config_name (the same draw proposed twice); they merge
  // into one node here, as they always have at campaign level.
  const byConfig = new Map<string, Record<string, unknown>[]>()
  for (const row of rows) {
    const configName = String(row.config_name ?? '')
    const list = byConfig.get(configName)
    if (list) list.push(row)
    else byConfig.set(configName, [row])
  }

  return [...byConfig.entries()].map(([configName, configRows]) => {
    const runs = configRows.map((row) => runNode(campaignId, batch, configName, row))
    const status = rollupStatuses(runs.map((r) => r.status))
    const passed = runs.filter((r) => r.status === 'passed').length
    const objective = configObjective(configRows)
    return {
      id: `${prefix}//cfg/${configName}`,
      // The objective is what a search's configs are read *by*, so it belongs on the label
      // rather than a tooltip. Absent for a batch-mode config and for a multi-objective
      // search, neither of which records a scalar.
      label: objective === null ? configName : `${configName}  [${fmtObjective(objective)}]`,
      kind: 'config' as const,
      campaignId,
      configName,
      ...(batch == null ? {} : { batch }),
      objective,
      status,
      // "0/0 passed" would be a misleading verdict on something that never ran.
      count: status === 'skipped' ? 'skipped' : `${passed}/${runs.length}`,
      children: runs,
    }
  })
}

function runNode(
  campaignId: string,
  batch: number | null,
  configName: string,
  row: Record<string, unknown>,
): ResultsTreeItem {
  const status = runStatus(row)
  // A composition-failed draw has a NULL run_id: there is no run behind it, so it gets
  // no run number and is not selectable — clicking through would open a run view for a
  // run that does not exist. Coercing the NULL to 0 would invent "run 0" instead.
  const composed = row.run_id !== null && row.run_id !== undefined
  if (!composed) {
    return {
      id: `${configPrefix(campaignId, batch)}//cfg/${configName}//not-composed`,
      label: 'not composed — parameters could not be realized',
      kind: 'placeholder',
      campaignId,
      configName,
      ...(batch == null ? {} : { batch }),
      status,
      disabled: true,
    }
  }
  const runId = Number(row.run_id)
  return {
    id: runNodeId(campaignId, batch, configName, runId),
    label: `run ${runId}`,
    kind: 'run',
    campaignId,
    configName,
    runId,
    ...(batch == null ? {} : { batch }),
    status,
  }
}

// A single non-selectable child shown under a campaign that has no queryable `data.db` yet.
export function placeholderChild(campaignId: string, message: string): ResultsTreeItem {
  return {
    id: `${campaignId}//placeholder`,
    label: message,
    kind: 'placeholder',
    campaignId,
    status: 'neutral',
    disabled: true,
  }
}

// The ancestor ids of a node id, outermost first — `c//cfg/x//run/3` → [`c`, `c//cfg/x`]. Node ids
// are built as `//`-joined paths (see above), so the ancestors are its prefixes. Used to open the
// tree on the current selection instead of showing it collapsed at campaign level.
export function ancestorIds(id: string): string[] {
  const parts = id.split('//')
  return parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join('//'))
}

// Flatten a tree into an id → item map so the click handler can resolve what was clicked without
// re-parsing ids.
export function indexById(items: ResultsTreeItem[]): Map<string, ResultsTreeItem> {
  const map = new Map<string, ResultsTreeItem>()
  const walk = (nodes: ResultsTreeItem[]) => {
    for (const n of nodes) {
      map.set(n.id, n)
      if (n.children) walk(n.children)
    }
  }
  walk(items)
  return map
}
