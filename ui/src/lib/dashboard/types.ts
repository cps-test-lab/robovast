// The panel framework's contracts. A panel plugin declares a manifest (its type name + layout
// defaults) and a component; the vast file declares an ordered list of panel specs (type + position +
// data bindings) that the PanelHost renders. Panels receive only the clock and the data provider, so
// they stay independent of both the layout engine and the data transport.

import type { ComponentType } from 'react'
import type { PlaybackClock } from './clock'
import type { DataProvider } from './dataProvider'
import type { RemoteDescriptor } from '@/lib/remote'

export type Anchor =
  | 'bottom'
  | 'top'
  | 'left'
  | 'right'
  | 'top-left'
  | 'top-right'
  | 'bottom-left'
  | 'bottom-right'
  | 'center'
  | 'fill'

export interface PanelPosition {
  anchor?: Anchor
  width?: number | string // pixels (number) or a CSS length ("40%")
  height?: number | string
}

/** A panel as declared in the vast, normalized: known fields lifted out, the rest (data bindings such
 *  as `layers`/`source`) kept verbatim in `config` for the panel plugin to interpret. */
export interface PanelSpec {
  type: string
  title?: string
  position: PanelPosition
  resizable: boolean
  minimizable: boolean
  minimized: boolean
  /** No Paper chrome (border/elevation/header) -- the panel body floats directly, e.g. the playback bar. */
  frameless: boolean
  hidden: boolean
  fixed: boolean
  /** When set, this panel is loaded at runtime as a Module-Federation remote (a package-provided
   *  panel like robovast_nav's `costmap`, or a user-authored `custom` panel) rather than from the
   *  built-in registry. The service attaches this descriptor in GET /campaigns/{id}/panels. */
  remote?: RemoteDescriptor
  config: Record<string, unknown>
}

/** Per-type defaults a panel plugin ships; the vast spec overrides these. */
export interface PanelManifest {
  type: string
  label: string
  defaultPosition: PanelPosition
  resizable?: boolean
  minimizable?: boolean
  frameless?: boolean
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

export interface PanelPlugin {
  manifest: PanelManifest
  component: ComponentType<PanelProps>
}
