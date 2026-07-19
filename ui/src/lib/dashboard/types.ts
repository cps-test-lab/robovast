// The panel framework's contracts. A panel plugin declares a manifest (its type name + layout
// defaults) and a component; the vast file declares an ordered list of panel specs (type + position +
// data bindings) that the PanelHost renders. Panels receive only the clock and the data provider, so
// they stay independent of both the layout engine and the data transport.

import type { ComponentType } from 'react'
import type { PlaybackClock } from './clock'
import type { DataProvider } from './dataProvider'

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

export interface PanelProps {
  spec: PanelSpec
  clock: PlaybackClock
  data: DataProvider
}

export interface PanelPlugin {
  manifest: PanelManifest
  component: ComponentType<PanelProps>
}
