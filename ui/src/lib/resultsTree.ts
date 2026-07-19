// Client-side model for the Results Explorer tree (campaign → config → run). Parallels
// `fileTree.ts`: pure, React-free helpers that turn the service's data into a tree the
// RichTreeView renders. Campaign-level status comes from `CampaignSummary`; the per-config /
// per-run breakdown is derived from rows of the campaign's `data.db` `runs` table (fetched via
// `queryCampaignDataSql`), since there is no dedicated per-run REST endpoint.

import type { CampaignSummary } from './robovastClient'

// Pass/fail status of any node, mapped to a theme color by `statusColor`. `neutral` = no verdict
// yet (e.g. a campaign that hasn't been postprocessed); `running` = still executing.
export type NodeStatus = 'passed' | 'failed' | 'unknown' | 'running' | 'neutral'

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

// Phases where the campaign is still working, so no final verdict exists yet.
const RUNNING_PHASES = new Set(['starting', 'running', 'finishing', 'postprocessing'])

// Map a NodeStatus onto a MUI theme palette path (used with `sx` color lookups).
export function statusColor(status: NodeStatus): string {
  switch (status) {
    case 'passed':
      return 'success.main'
    case 'failed':
      return 'error.main'
    case 'running':
      return 'warning.main'
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
  if (RUNNING_PHASES.has(c.phase)) return 'running'
  if (c.phase === 'failed' || c.num_failed > 0) return 'failed'
  if (c.postprocessed && c.num_passed > 0 && c.num_failed === 0) return 'passed'
  return 'neutral'
}

// A single run's verdict, from the `runs.status` column (passed | error | failed | unknown).
export function runStatus(row: Record<string, unknown>): NodeStatus {
  const s = String(row.status ?? '').toLowerCase()
  if (s === 'passed') return 'passed'
  if (s === 'failed' || s === 'error') return 'failed'
  return 'unknown'
}

// A config's rollup from its runs: any failure → failed; all passed → passed; else unknown.
function rollupConfig(runs: NodeStatus[]): NodeStatus {
  if (runs.some((s) => s === 'failed')) return 'failed'
  if (runs.length > 0 && runs.every((s) => s === 'passed')) return 'passed'
  return 'unknown'
}

// The top-level label/status for a campaign node (children are attached lazily on expand).
export function campaignItem(c: CampaignSummary): ResultsTreeItem {
  return {
    id: c.campaign_id,
    label: c.campaign_id,
    kind: 'campaign',
    campaignId: c.campaign_id,
    status: campaignStatus(c),
    count: c.num_runs > 0 ? `${c.num_passed}/${c.num_runs}` : undefined,
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
    const runId = Number(row.run_id ?? 0)
    const status = runStatus(row)
    const runNode: ResultsTreeItem = {
      id: `${campaignId}//cfg/${configName}//run/${runId}`,
      label: `run ${runId}`,
      kind: 'run',
      campaignId,
      configName,
      runId,
      status,
    }
    const list = byConfig.get(configName)
    if (list) list.push(runNode)
    else byConfig.set(configName, [runNode])
  }

  return [...byConfig.entries()].map(([configName, runs]) => {
    const passed = runs.filter((r) => r.status === 'passed').length
    return {
      id: `${campaignId}//cfg/${configName}`,
      label: configName,
      kind: 'config',
      campaignId,
      configName,
      status: rollupConfig(runs.map((r) => r.status)),
      count: `${passed}/${runs.length}`,
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
