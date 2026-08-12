// Client-side model for the Results Explorer tree (campaign → config → run). Parallels
// `fileTree.ts`: pure, React-free helpers that turn the service's data into a tree the
// RichTreeView renders. Campaign-level status comes from `CampaignSummary`; the per-config /
// per-run breakdown is derived from rows of the campaign's `data.db` `runs` table (fetched via
// `queryCampaignDataSql`), since there is no dedicated per-run REST endpoint.

import { isFailed, isRunning, type CampaignSummary } from './robovastClient'

// Pass/fail status of any node, mapped to a theme color by `statusColor`. `neutral` = no verdict
// yet (e.g. a campaign that hasn't been postprocessed); `running` = still executing; `skipped` = a
// search parameter set whose configuration could not be built at all, so it never ran — distinct
// from `failed` (which ran and lost) and from `unknown` (which may yet have a verdict).
export type NodeStatus = 'passed' | 'failed' | 'skipped' | 'unknown' | 'running' | 'neutral'

export type NodeKind = 'campaign' | 'config' | 'run' | 'placeholder'

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
  /** e.g. "5/5" (passed/total) shown as a trailing chip; omitted for leaves/placeholders. */
  count?: string
  disabled?: boolean
  children?: ResultsTreeItem[]
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

// A single run's verdict, from the `runs.status` column (passed | error | failed |
// composition_failed | unknown).
export function runStatus(row: Record<string, unknown>): NodeStatus {
  const s = String(row.status ?? '').toLowerCase()
  if (s === 'passed') return 'passed'
  if (s === 'failed' || s === 'error') return 'failed'
  if (s === 'composition_failed') return 'skipped'
  return 'unknown'
}

// A config's rollup from its runs: any failure → failed; all passed → passed; all skipped →
// skipped (the draw never became runnable, so there is nothing pending); else unknown.
function rollupConfig(runs: NodeStatus[]): NodeStatus {
  if (runs.some((s) => s === 'failed')) return 'failed'
  if (runs.length > 0 && runs.every((s) => s === 'passed')) return 'passed'
  if (runs.length > 0 && runs.every((s) => s === 'skipped')) return 'skipped'
  return 'unknown'
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

// Build the config → run subtree for one campaign from its `runs` rows (already ordered by
// config_name, run_id). Configs preserve first-seen order; each carries a passed/total count.
export function buildCampaignChildren(
  campaignId: string,
  rows: Record<string, unknown>[],
): ResultsTreeItem[] {
  const byConfig = new Map<string, ResultsTreeItem[]>()
  for (const row of rows) {
    const configName = String(row.config_name ?? '')
    const status = runStatus(row)
    // A composition-failed draw has a NULL run_id: there is no run behind it, so it gets
    // no run number and is not selectable — clicking through would open a run view for a
    // run that does not exist. Coercing the NULL to 0 would invent "run 0" instead.
    const composed = row.run_id !== null && row.run_id !== undefined
    const runId = composed ? Number(row.run_id) : undefined
    const runNode: ResultsTreeItem = composed
      ? {
          id: `${campaignId}//cfg/${configName}//run/${runId}`,
          label: `run ${runId}`,
          kind: 'run',
          campaignId,
          configName,
          runId,
          status,
        }
      : {
          id: `${campaignId}//cfg/${configName}//not-composed`,
          label: 'not composed — parameters could not be realized',
          kind: 'placeholder',
          campaignId,
          configName,
          status,
          disabled: true,
        }
    const list = byConfig.get(configName)
    if (list) list.push(runNode)
    else byConfig.set(configName, [runNode])
  }

  return [...byConfig.entries()].map(([configName, runs]) => {
    const status = rollupConfig(runs.map((r) => r.status))
    const passed = runs.filter((r) => r.status === 'passed').length
    return {
      id: `${campaignId}//cfg/${configName}`,
      label: configName,
      kind: 'config',
      campaignId,
      configName,
      status,
      // "0/0 passed" would be a misleading verdict on something that never ran.
      count: status === 'skipped' ? 'skipped' : `${passed}/${runs.length}`,
      children: runs,
    }
  })
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
