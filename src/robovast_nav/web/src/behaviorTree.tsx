// Nav2BehaviorTreePanel: a live-updating view of nav2's behavior tree. It reads a
// `behaviors`-schema table (default `nav2_behaviors`, produced by robovast_nav's Nav2BtTree
// postprocessing plugin), rebuilds the tree from each node's parent_id, and colors every node
// by its status at the current playback time. As the clock moves, node colors update.
//
// This is the tree-rendering logic of the core UI's ScenarioTreePanel, re-implemented in plain
// React (no MUI / react-query) because a Module-Federation remote shares only react/react-dom
// with the host. It ships with robovast_nav and reads its data through the host-injected
// DataProvider's generic `series` seam -- no dedicated service endpoint.

import { useEffect, useState } from 'react'
import type { DataProvider, PanelProps } from './contract'

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
    columns: ['timestamp', 'behavior_name', 'behavior_id', 'parent_id', 'status_name', 'class_name'],
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

// BT.CPP / py_trees statuses -> a color. Latest event with t <= now wins; none yet = not-ticked.
const STATUS_COLOR: Record<string, string> = {
  RUNNING: '#f0b429',
  SUCCESS: '#4caf50',
  FAILURE: '#ef5350',
  INVALID: '#6b7280',
  IDLE: '#6b7280',
  SKIPPED: '#6b7280',
}

function statusAt(node: TreeNode, now: number): string {
  let status = 'INVALID'
  for (const e of node.events) {
    if (e.t <= now) status = e.status
    else break
  }
  return status
}

/** Current playback time, kept in sync with the host clock. */
function useClockTime(clock: PanelProps['clock']): number {
  const [t, setT] = useState(clock.t)
  useEffect(() => {
    setT(clock.t)
    return clock.subscribe(() => setT(clock.t))
  }, [clock])
  return t
}

const box: React.CSSProperties = { height: '100%', overflow: 'auto', padding: 6, fontSize: 13 }
const note: React.CSSProperties = { margin: 8, color: '#b26a00', fontSize: 12, lineHeight: 1.4 }

export default function Nav2BehaviorTreePanel({ spec, clock, data }: PanelProps) {
  const table = ((spec.config.source ?? {}) as { table?: string }).table ?? 'nav2_behaviors'
  const t = useClockTime(clock)

  const [tree, setTree] = useState<TreeData | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    setTree(null)
    setError(null)
    loadTree(data, table).then(
      (d) => !cancelled && setTree(d),
      (e) => !cancelled && setError(e instanceof Error ? e.message : String(e)),
    )
    return () => {
      cancelled = true
    }
  }, [data, table])

  if (error) return <div style={{ ...note, color: '#c62828' }}>{error}</div>
  if (!tree) return <div style={note}>Loading behavior tree…</div>
  if (tree.kind === 'missing')
    return (
      <div style={note}>
        No <code>{tree.table}</code> data. Add <code>/behavior_tree_log</code> to the scenario's{' '}
        <code>bag_record(...)</code>, and <code>rosbags_nav2bt_to_csv</code> + <code>nav2_bt_tree</code>{' '}
        to postprocessing.
      </div>
    )
  if (tree.kind === 'stale')
    return (
      <div style={note}>
        This <code>{tree.table}</code> table has no <code>parent_id</code> — re-run postprocessing to
        capture the tree structure.
      </div>
    )

  const renderNode = (id: string, depth: number): React.ReactNode => {
    const node = tree.nodes.get(id)!
    const status = statusAt(node, t)
    const kids = tree.childrenOf.get(id) ?? []
    return (
      <div key={id}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '1px 0', paddingLeft: depth * 14 }}>
          <span
            style={{
              width: 9,
              height: 9,
              borderRadius: '50%',
              flexShrink: 0,
              background: STATUS_COLOR[status] ?? STATUS_COLOR.INVALID,
            }}
          />
          <span>{node.name}</span>
          {node.className && node.className !== node.name ? (
            <span style={{ fontSize: 11, color: '#9aa0a6' }}>{node.className}</span>
          ) : null}
        </div>
        {kids.map((c) => renderNode(c, depth + 1))}
      </div>
    )
  }

  return <div style={box}>{tree.roots.map((id) => renderNode(id, 0))}</div>
}
