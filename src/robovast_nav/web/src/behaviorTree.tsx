// Nav2BehaviorTreePanel: nav2's *internal* behavior tree in the run view, live-coloured by node
// status. It renders the host's built-in scenario-tree panel rather than a tree of its own --
// what nav2 needs is that panel pointed at a different table, with its own title and its own
// answer to "there is no data": the scenario tree's answer talks about --bt-log, which has
// nothing to do with producing nav2's tree.
//
// Everything on screen therefore comes from the built-in panel, and improvements to it (child
// ordering, node-kind glyphs, feedback messages, source lines, scrolling) reach this panel with
// no work here. This file only supplies defaults.
//
// The component arrives through props: a Module-Federation remote shares only react/react-dom
// with the host, so it cannot import the host's modules. `builtins` is optional in the contract,
// so a host predating it degrades to a message instead of a blank panel.

import type { PanelProps } from './contract'

/** The table robovast_nav's Nav2BtTree postprocessing writes, in the shared behaviors schema. */
const DEFAULT_TABLE = 'nav2_behaviors'

/** How to produce that table -- nav2's own recording and postprocessing, not the scenario's. */
const MISSING_HINT =
  "Add /behavior_tree_log to the scenario's bag_record(...), and rosbags_nav2bt_to_csv + " +
  'nav2_bt_tree to postprocessing.'

const note: React.CSSProperties = { margin: 8, color: '#b26a00', fontSize: 12, lineHeight: 1.4 }

export default function Nav2BehaviorTreePanel(props: PanelProps) {
  const ScenarioTree = props.builtins?.ScenarioTree
  if (!ScenarioTree) {
    return (
      <div style={note}>
        This panel needs a newer robovast web UI (it renders the built-in scenario tree, which
        this host does not offer to package panels).
      </div>
    )
  }
  // The campaign's own config wins: spread last, so a .vast may still point this panel at
  // another table or reword the hint.
  const spec = {
    ...props.spec,
    config: {
      source: { table: DEFAULT_TABLE },
      missing_hint: MISSING_HINT,
      ...props.spec.config,
    },
  }
  return <ScenarioTree {...props} spec={spec} />
}
