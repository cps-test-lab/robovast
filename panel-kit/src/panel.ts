// The panel contract: what a run-view panel receives. A panel gets only the clock and the data
// provider, so it stays independent of both the layout engine and the data transport.
//
// This is the *panel-facing* half. The layout fields the host needs (anchor/size, resizable,
// minimized, the Module-Federation descriptor) are deliberately absent: no panel reads them, and
// including them would drag the host's remote-loading types in here. The host extends `PanelSpec`
// with those in ui/src/lib/dashboard/types.ts.

import type { ComponentType } from 'react'
import type { PlaybackClock } from './clock'
import type { DataProvider } from './dataProvider'

/** A panel as declared in the vast, as far as the panel itself is concerned: its type, its title, and
 *  its data bindings kept verbatim in `config` for the panel to interpret. */
export interface PanelSpec {
  type: string
  title?: string
  config: Record<string, unknown>
}

/** Built-in panels a package-provided panel can render instead of reimplementing.
 *
 *  A package whose data is a table in an existing schema usually wants a built-in panel with
 *  different defaults, not a different renderer -- robovast_nav's nav2 behavior tree is the
 *  scenario tree pointed at `nav2_behaviors`, with its own title and empty-state guidance. A
 *  Module-Federation remote shares only react/react-dom with the host and so cannot import the
 *  component, which is why it arrives as a prop.
 *
 *  Optional throughout: built-in panels do not need it, and a remote built against an older
 *  host must degrade rather than crash.
 */
export interface PanelBuiltins {
  ScenarioTree?: ComponentType<PanelProps>
}

export interface PanelProps {
  spec: PanelSpec
  clock: PlaybackClock
  data: DataProvider
  builtins?: PanelBuiltins
}
