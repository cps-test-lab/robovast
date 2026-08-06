// ScenarioTreePanel: an rviz-scenario-execution-style view of the behaviour tree. It reads the
// `behaviors` table (from the behaviors.jsonl scenario_execution writes under execution.bt_log),
// rebuilds the tree from each node's parent_id, and colours every node by its status at the current
// playback time. As the clock moves, node colours update.
//
// Report-if-missing (no fallback rendering): if the table is absent the campaign didn't enable
// bt_log; if it lacks parent_id the campaign was postprocessed before tree structure was captured.
// Each case shows the concrete fix.

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { SimpleTreeView } from '@mui/x-tree-view/SimpleTreeView'
import { TreeItem } from '@mui/x-tree-view/TreeItem'
import { registerPanel } from '@/lib/dashboard/registry'
import { useClock } from '@/lib/dashboard/clock'
import type { DataProvider } from '@/lib/dashboard/dataProvider'
import type { PanelProps } from '@/lib/dashboard/types'

interface TreeNode {
  id: string
  name: string
  className: string
  parent: string
  events: { t: number; status: string }[] // sorted by t
}

type TreeData =
  | { kind: 'missing'; table: string }
  | { kind: 'stale'; table: string }
  | { kind: 'ok'; nodes: Map<string, TreeNode>; childrenOf: Map<string, string[]>; roots: string[] }

async function loadTree(data: DataProvider, table: string): Promise<TreeData> {
  if (!(await data.has(table))) return { kind: 'missing', table }
  if (!(await data.has(table, ['parent_id']))) return { kind: 'stale', table }

  const rows = await data.series(table, {
    columns: ['timestamp', 'behavior_name', 'behavior_id', 'parent_id', 'status_name'],
  })

  const nodes = new Map<string, TreeNode>()
  for (const r of rows) {
    const id = String(r.behavior_id ?? '')
    if (!id) continue
    let n = nodes.get(id)
    if (!n) {
      n = {
        id,
        name: String(r.behavior_name ?? id),
        className: String(r.class_name ?? ''),
        parent: String(r.parent_id ?? ''),
        events: [],
      }
      nodes.set(id, n)
    }
    n.events.push({ t: Number(r.timestamp), status: String(r.status_name ?? 'INVALID') })
  }

  const childrenOf = new Map<string, string[]>()
  const roots: string[] = []
  for (const n of nodes.values()) {
    n.events.sort((a, b) => a.t - b.t)
    if (n.parent && nodes.has(n.parent)) {
      const arr = childrenOf.get(n.parent) ?? []
      arr.push(n.id)
      childrenOf.set(n.parent, arr)
    } else {
      roots.push(n.id)
    }
  }
  return { kind: 'ok', nodes, childrenOf, roots }
}

// nav2/py_trees statuses -> a colour. Latest event with t <= now wins; none yet = not-ticked.
const STATUS_COLOR: Record<string, string> = {
  RUNNING: '#f0b429',
  SUCCESS: '#4caf50',
  FAILURE: '#ef5350',
  INVALID: '#6b7280',
}

function statusAt(node: TreeNode, now: number): string {
  let status = 'INVALID'
  for (const e of node.events) {
    if (e.t <= now) status = e.status
    else break
  }
  return status
}

function ScenarioTreePanel({ spec, clock, data }: PanelProps) {
  const source = (spec.config.source ?? {}) as { table?: string }
  const table = source.table ?? 'behaviors'
  const { t } = useClock(clock)

  const tree = useQuery({
    queryKey: ['scenario-tree', data.scope, table],
    queryFn: () => loadTree(data, table),
    retry: false,
  })

  const allIds = useMemo(
    () => (tree.data?.kind === 'ok' ? [...tree.data.nodes.keys()] : []),
    [tree.data],
  )

  if (tree.isPending) return <CircularProgress size={20} sx={{ m: 2 }} />
  if (tree.isError)
    return (
      <Alert severity="error" sx={{ m: 1 }}>
        {(tree.error as Error).message}
      </Alert>
    )

  const d = tree.data
  if (d.kind === 'missing')
    return (
      <Alert severity="warning" sx={{ m: 1 }}>
        No <code>{d.table}</code> data. Set <code>bt_log: true</code> under <code>execution:</code> in
        the campaign's <code>.vast</code> to record the behaviour tree.
      </Alert>
    )
  if (d.kind === 'stale')
    return (
      <Alert severity="warning" sx={{ m: 1 }}>
        This <code>{d.table}</code> table has no <code>parent_id</code> — re-run postprocessing to
        capture the tree structure.
      </Alert>
    )

  const renderNode = (id: string): React.ReactNode => {
    const node = d.nodes.get(id)!
    const status = statusAt(node, t)
    return (
      <TreeItem
        key={id}
        itemId={id}
        label={
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 0.25 }}>
            <Box
              sx={{
                width: 9,
                height: 9,
                borderRadius: '50%',
                bgcolor: STATUS_COLOR[status] ?? STATUS_COLOR.INVALID,
                flexShrink: 0,
              }}
            />
            <Box component="span" sx={{ fontSize: 13 }}>
              {node.name}
            </Box>
            {node.className ? (
              <Box component="span" sx={{ fontSize: 11, color: 'text.secondary' }}>
                {node.className}
              </Box>
            ) : null}
          </Box>
        }
      >
        {(d.childrenOf.get(id) ?? []).map(renderNode)}
      </TreeItem>
    )
  }

  return (
    <Box sx={{ height: '100%', overflow: 'auto', p: 0.5 }}>
      <SimpleTreeView defaultExpandedItems={allIds} sx={{ '& .MuiTreeItem-content': { py: 0 } }}>
        {d.roots.map(renderNode)}
      </SimpleTreeView>
    </Box>
  )
}

registerPanel({
  manifest: {
    type: 'scenario_tree',
    label: 'Scenario tree',
    defaultPosition: { anchor: 'left', width: 320 },
    resizable: true,
    minimizable: true,
  },
  component: ScenarioTreePanel,
})

export default ScenarioTreePanel
