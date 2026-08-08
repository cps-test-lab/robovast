// ScenarioTreePanel: an rviz-scenario-execution-style view of a behaviour tree. It reads a
// `behaviors`-schema table -- `behaviors` itself (written by scenario_execution's --bt-log, on by
// default) or any other via `source: { table }`, which is how nav2's `nav2_behaviors` is shown --
// rebuilds the tree, and colours every node by its status at the current playback time.
//
// The schema grew: alongside the original seven columns a row may carry child_index, type,
// additional_detail, feedback_message, is_active, tip_id and osc_file/osc_line/osc_column. Every one
// of those is optional and probed for, because a table produced by a different route (nav2_behaviors,
// or a campaign postprocessed before the columns existed) has only the original seven and must still
// render. What each column buys is noted where it is used.
//
// Report-if-missing (no fallback rendering): if the table is absent nothing recorded a tree; if it
// lacks parent_id the campaign was postprocessed before tree structure was captured. Each case shows
// the concrete fix.

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import Tooltip from '@mui/material/Tooltip'
import { SimpleTreeView } from '@mui/x-tree-view/SimpleTreeView'
import { TreeItem } from '@mui/x-tree-view/TreeItem'
import AltRouteRounded from '@mui/icons-material/AltRouteRounded'
import ArrowRightAltRounded from '@mui/icons-material/ArrowRightAltRounded'
import ChangeCircleRounded from '@mui/icons-material/ChangeCircleRounded'
import SplitscreenRounded from '@mui/icons-material/SplitscreenRounded'
import { registerPanel } from '@/lib/dashboard/registry'
import { timeSeriesFromRows, type TimeSeriesSource } from '@/lib/dashboard/timeSeries'
import { useClock, type DataProvider, type DataRow, type PanelProps } from '@robovast/panel-kit'

// Always present -- the seven columns every behaviours table has had.
const CORE_COLUMNS = [
  'timestamp',
  'behavior_name',
  'behavior_id',
  'parent_id',
  'status_name',
  'class_name',
]
// Present only for tables written by --bt-log. Requested individually so a table with some but
// not all of them still gets what it has.
const OPTIONAL_COLUMNS = [
  'child_index',
  'type',
  'additional_detail',
  'feedback_message',
  'tip_id',
  'osc_file',
  'osc_line',
]

interface TreeNode {
  id: string
  name: string
  className: string
  parent: string
  /** Position among the parent's children. null when the table predates the column. */
  childIndex: number | null
  type: string
  additionalDetail: string
  oscFile: string
  oscLine: string
  /** Every row for this node, indexed by time. `.at(t)` is a binary search. */
  series: TimeSeriesSource
  /** Time of this node's first row: before it, the node had not been recorded yet. */
  firstT: number
}

type TreeData =
  | { kind: 'missing'; table: string }
  | { kind: 'stale'; table: string }
  | { kind: 'ok'; nodes: Map<string, TreeNode>; childrenOf: Map<string, string[]>; roots: string[] }

async function loadTree(data: DataProvider, table: string): Promise<TreeData> {
  if (!(await data.has(table))) return { kind: 'missing', table }
  if (!(await data.has(table, ['parent_id']))) return { kind: 'stale', table }

  const present = await Promise.all(
    OPTIONAL_COLUMNS.map(async (c) => ((await data.has(table, [c])) ? c : null)),
  )
  const columns = [...CORE_COLUMNS, ...present.filter((c): c is string => c !== null)]
  const rows = await data.series(table, { columns })

  // One bucket of rows per node, then one TimeSeriesSource over each. Reusing the shared
  // builder gets sorting and a binary-search `.at(t)` for free, and -- because it returns the
  // whole row -- the feedback message and tip alongside the status in one lookup.
  const rowsById = new Map<string, DataRow[]>()
  for (const r of rows) {
    const id = String(r.behavior_id ?? '')
    if (!id) continue
    const bucket = rowsById.get(id)
    if (bucket) bucket.push(r)
    else rowsById.set(id, [r])
  }

  const nodes = new Map<string, TreeNode>()
  for (const [id, nodeRows] of rowsById) {
    const first = nodeRows[0]
    const series = timeSeriesFromRows(nodeRows)
    const range = series.range()
    nodes.set(id, {
      id,
      name: String(first.behavior_name ?? id),
      className: String(first.class_name ?? ''),
      parent: String(first.parent_id ?? ''),
      childIndex: first.child_index == null ? null : Number(first.child_index),
      type: String(first.type ?? ''),
      additionalDetail: String(first.additional_detail ?? ''),
      oscFile: String(first.osc_file ?? ''),
      oscLine: first.osc_line == null ? '' : String(first.osc_line),
      series,
      firstT: range ? range[0] : 0,
    })
  }

  const childrenOf = new Map<string, string[]>()
  const roots: string[] = []
  for (const n of nodes.values()) {
    if (n.parent && nodes.has(n.parent)) {
      const arr = childrenOf.get(n.parent) ?? []
      arr.push(n.id)
      childrenOf.set(n.parent, arr)
    } else {
      roots.push(n.id)
    }
  }
  // Row-arrival order is the order statuses happened to change, which for a sequence is not the
  // order its children are declared in. child_index restores the declared order; without the
  // column there is nothing better than arrival order to fall back to.
  for (const siblings of childrenOf.values()) {
    siblings.sort((a, b) => {
      const ai = nodes.get(a)!.childIndex
      const bi = nodes.get(b)!.childIndex
      if (ai == null || bi == null) return 0
      return ai - bi
    })
  }
  return { kind: 'ok', nodes, childrenOf, roots }
}

// nav2/py_trees statuses -> a colour. Latest row with t <= now wins; none yet = not-ticked.
const STATUS_COLOR: Record<string, string> = {
  RUNNING: '#f0b429',
  SUCCESS: '#4caf50',
  FAILURE: '#ef5350',
  INVALID: '#6b7280',
}

// Composite kind -> glyph, so the shape of the tree is readable without reading class names.
// A plain behaviour gets none: the status dot already marks it.
const TYPE_ICON: Record<string, typeof AltRouteRounded> = {
  SEQUENCE: ArrowRightAltRounded,
  SELECTOR: AltRouteRounded,
  PARALLEL: SplitscreenRounded,
  DECORATOR: ChangeCircleRounded,
}

/** The node's recorded row at *t*, or null before it was first recorded.
 *
 *  `.at(t)` clamps to the earliest sample, which is right for a --bt-log table (its earliest
 *  sample is the INVALID snapshot at timestamp 0) but would show a nav2_behaviors node its
 *  first status before that status happened -- hence the explicit check.
 */
function rowAt(node: TreeNode, t: number): DataRow | null {
  if (t < node.firstT) return null
  return node.series.at(t)
}

//: How to produce the table, said when the run has none. Overridable via `missing_hint` in the
//: panel's config, because the answer depends on which table the panel was pointed at: a
//: derived panel (robovast_nav's nav2 tree) names its own postprocessing rather than bt_log,
//: which has nothing to do with it. Keeps this panel from knowing about any particular producer.
const DEFAULT_MISSING_HINT =
  'The behaviour tree is recorded by default; an execution image whose scenario_execution ' +
  'predates --bt-log produces none, as does bt_log: false under execution:.'

function ScenarioTreePanel({ spec, clock, data }: PanelProps) {
  const source = (spec.config.source ?? {}) as { table?: string }
  const table = source.table ?? 'behaviors'
  const missingHint = String(spec.config.missing_hint ?? DEFAULT_MISSING_HINT)
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

  // Which action is responsible for a failing tree. py_trees' tip() is the deepest node that was
  // running before the traversal turned back, so a failing root names its culprit directly --
  // otherwise found by expanding the tree and hunting for the red leaf.
  const culprit = useMemo(() => {
    if (tree.data?.kind !== 'ok') return null
    for (const rootId of tree.data.roots) {
      const root = tree.data.nodes.get(rootId)!
      const row = rowAt(root, t)
      if (String(row?.status_name ?? '') !== 'FAILURE') continue
      const tipId = row?.tip_id == null ? '' : String(row.tip_id)
      const tip = tipId ? tree.data.nodes.get(tipId) : undefined
      if (tip) return { tip, message: String(rowAt(tip, t)?.feedback_message ?? '') }
    }
    return null
  }, [tree.data, t])

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
        No <code>{d.table}</code> data. {missingHint}
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
    const row = rowAt(node, t)
    const status = String(row?.status_name ?? 'INVALID')
    const feedback = String(row?.feedback_message ?? '')
    const Glyph = TYPE_ICON[node.type]
    const where = node.oscLine
      ? `${node.oscFile.split('/').pop() ?? node.oscFile}:${node.oscLine}`
      : ''
    const tip = [node.className, node.additionalDetail, where].filter(Boolean).join(' · ')

    // Nothing wraps: a node stays one row tall however narrow the panel is, so the tree's
    // depth stays readable and the overflow becomes horizontal scroll instead of reflow.
    const label = (
      <Box sx={{ py: 0.25, whiteSpace: 'nowrap' }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
          <Box
            sx={{
              width: 9,
              height: 9,
              borderRadius: '50%',
              bgcolor: STATUS_COLOR[status] ?? STATUS_COLOR.INVALID,
              flexShrink: 0,
            }}
          />
          {Glyph ? (
            <Glyph sx={{ fontSize: 14, color: 'text.disabled', flexShrink: 0 }} />
          ) : null}
          <Box component="span" sx={{ fontSize: 13 }}>
            {node.name}
          </Box>
          {node.className ? (
            <Box component="span" sx={{ fontSize: 11, color: 'text.secondary' }}>
              {node.className.split('.').pop()}
            </Box>
          ) : null}
        </Box>
        {feedback ? (
          // Capped: an action that reports a long message would otherwise make one node
          // as tall as the panel. The full text is in the tooltip.
          <Box
            component="span"
            sx={{
              display: 'block',
              pl: 2.25,
              fontSize: 11,
              color: 'text.secondary',
              maxWidth: 280,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {feedback}
          </Box>
        ) : null}
      </Box>
    )

    return (
      <TreeItem
        key={id}
        itemId={id}
        label={tip ? <Tooltip title={tip} placement="right" children={label} /> : label}
      >
        {(d.childrenOf.get(id) ?? []).map(renderNode)}
      </TreeItem>
    )
  }

  // A real scenario's tree is routinely taller and wider than the panel: every node is expanded
  // by default, and depth costs horizontal indent. The column below is what makes that usable --
  // the banner keeps its place while the tree scrolls under it, so the reason a run failed does
  // not scroll away just as you go looking for the node that caused it.
  return (
    <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      {culprit ? (
        <Alert severity="error" sx={{ m: 0.5, mb: 0, py: 0, fontSize: 12, flexShrink: 0 }}>
          Failed at <strong>{culprit.tip.name}</strong>
          {culprit.tip.oscLine
            ? ` (${culprit.tip.oscFile.split('/').pop()}:${culprit.tip.oscLine})`
            : ''}
          {culprit.message ? ` — ${culprit.message}` : ''}
        </Alert>
      ) : null}
      {/* minHeight/minWidth 0 let this shrink inside the flex column so `auto` actually
          overflows; without them a flex child is floored at its content size and the tree is
          clipped instead of scrolled. */}
      <Box sx={{ flexGrow: 1, minHeight: 0, minWidth: 0, overflow: 'auto', p: 0.5 }}>
        <SimpleTreeView
          defaultExpandedItems={allIds}
          sx={{
            '& .MuiTreeItem-content': { py: 0 },
            // Grow past the panel instead of compressing into it, so a deep tree scrolls
            // sideways with its indentation intact.
            minWidth: 'max-content',
          }}
        >
          {d.roots.map(renderNode)}
        </SimpleTreeView>
      </Box>
    </Box>
  )
}

registerPanel({
  manifest: {
    type: 'scenario_tree',
    label: 'Scenario',
    defaultPosition: { anchor: 'left', width: 320 },
    resizable: true,
    minimizable: true,
  },
  component: ScenarioTreePanel,
})

export default ScenarioTreePanel
