// The panel framework's *host-side* contracts: what the layout engine and the plugin registry need.
// A panel plugin declares a manifest (its type name + layout defaults) and a component; the vast file
// declares an ordered list of panel specs (type + position + data bindings) that the PanelHost renders.
//
// What a panel itself receives -- PanelProps/ConfigPanelProps, PanelBuiltins, and the panel-facing
// PanelSpec -- lives in
// @robovast/panel-kit, shared with package-provided panel remotes that cannot import from here. This
// file extends that spec with the layout and remote-descriptor fields no panel reads.

import type { ComponentType } from 'react'
import type { ConfigPanelProps, PanelProps, PanelSpec as PanelSpecBase } from '@robovast/panel-kit'
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
  /** Centred along an edge, floating *above* that edge's reserved band rather than docking
   *  into it -- which is what lets a panel sit over the playback bar's own `bottom` anchor.
   *  Needs a declared size; a full-width `bottom-center` would just be a `bottom` dock. */
  | 'top-center'
  | 'bottom-center'
  | 'left-center'
  | 'right-center'
  | 'center'

export interface PanelPosition {
  anchor?: Anchor
  width?: number | string // pixels (number) or a CSS length ("40%")
  height?: number | string
  /** Occupy the rectangle the docks leave over. Used instead of an `anchor`, not with one. */
  fill?: boolean
}

/** A panel as declared in the vast, normalized: known fields lifted out, the rest (data bindings such
 *  as `layers`/`source`) kept verbatim in `config` for the panel plugin to interpret.
 *
 *  Extends the kit's panel-facing spec (`type`/`title`/`config`) with everything only the host acts on. */
export interface PanelSpec extends PanelSpecBase {
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

/** PanelProps as the *host* constructs them: the same contract a panel sees, but carrying the fuller
 *  spec above. Host-internal components (the remote loader, the frame chrome) need the layout and
 *  remote-descriptor fields; a panel never does, which is why the shared contract omits them. */
export interface HostPanelProps extends PanelProps {
  spec: PanelSpec
}

export interface PanelPlugin {
  manifest: PanelManifest
  component: ComponentType<PanelProps>
}

// -- the config view --------------------------------------------------------
//
// The Config tab's third column. A far smaller layout grammar than the run view's: one column,
// so a panel declares a height and nothing else. No anchors, no drag, no resize -- the order and
// the sizes are the campaign author's, written in the .vast.

/** A config panel as declared in the vast, normalized. Extends the kit's panel-facing spec with
 *  the two fields only the host acts on. */
export interface ConfigPanelSpec extends PanelSpecBase {
  /** Pixels (number) or a fraction of the column ("35%"). Undefined takes the remaining space,
   *  which only the last panel may do. */
  height?: number | string
  hidden: boolean
  /** Set when this panel is loaded at runtime as a Module-Federation remote. */
  remote?: RemoteDescriptor
}

/** Per-type defaults a config panel ships; the vast spec overrides them. */
export interface ConfigPanelManifest {
  type: string
  label: string
  defaultHeight?: number | string
}

export interface ConfigPanelPlugin {
  manifest: ConfigPanelManifest
  component: ComponentType<ConfigPanelProps>
}
