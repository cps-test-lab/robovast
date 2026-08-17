// The config-panel contract: what a panel in the Config tab's third column receives.
//
// A sibling of PanelProps rather than a variant of it. The two surfaces answer different
// questions -- a run-view panel replays one run over a clock, a config panel shows what one
// *generated configuration* contains before anything has run -- so the props differ where they
// must: no PlaybackClock, and a resolved configuration instead of a run-scoped DataProvider.
// Everything else (the spec, the verbatim `config` bindings) is the same `PanelSpec` the run
// view uses, because "which type, titled what, with these bindings" is the same question.

import type { PanelSpec } from './panel'

/** One marker a variation contributed, in world coordinates.
 *
 *  Deliberately geometry rather than domain vocabulary -- a box at a pose, not "an obstacle" --
 *  so a panel draws a variation it has never heard of. Mirrors
 *  robovast.common.scene_markers.SceneMarker; the fields a `kind` does not use are absent. */
export interface SceneMarker {
  kind: 'box' | 'cylinder' | 'sphere' | 'pose' | 'path' | 'point'
  // Every optional field is `| null` as well as absent: these arrive from a pydantic model whose
  // unset Optionals serialize as null, so a type that said only `?` would be a description of
  // something the wire never sends.
  /** [x, y] or [x, y, z]. Absent on `path`, which carries `points`. */
  pos?: number[] | null
  /** `box`: full extents [x, y, z]. */
  size?: number[] | null
  radius?: number | null
  height?: number | null
  /** Rotation about z, radians. */
  yaw?: number | null
  /** `path`: the polyline, in order. */
  points?: number[][] | null
  label?: string
  /** CSS colour; empty lets the panel choose. */
  color?: string
  /** Markers sharing a group are shown and hidden together. */
  group?: string
}

/** What the variations of one configuration contributed to draw. */
export interface ConfigViewContribution {
  markers: SceneMarker[]
  /** Named workspace-relative paths, e.g. `{map: 'environments/office/map.yaml'}`. */
  files: Record<string, string>
  /** A contribution hook that raised, named. Shown rather than swallowed: a view missing one
   *  variation's markers otherwise looks like a variation that placed nothing. */
  errors: string[]
}

/** One variation's preview descriptor: the factor this configuration came from. */
export interface VariationPreview {
  variation_type: string
  params: Record<string, unknown>
  /** A Module-Federation descriptor when the variation's plugin ships a web preview; null when
   *  the type renders host-native. */
  remote?: { name: string; remote_entry_url: string; module: string } | null
}

/** One resolved configuration, as a config panel sees it. */
export interface ResolvedConfiguration {
  name: string
  /** The scenario parameters — what the trial is given. */
  parameters: Record<string, unknown>
  /** The resolved `sim` block — the world it runs in, and the overrides on it. */
  sim: Record<string, unknown>
  /** The `_`-prefixed keys a variation wrote for other readers. */
  internals: Record<string, unknown>
  contribution: ConfigViewContribution
  /** The factors this configuration came from, for a panel that wants to show them. */
  previews?: VariationPreview[]
}

export interface ConfigPanelProps {
  spec: PanelSpec
  /** The configuration currently selected in the middle column. */
  config: ResolvedConfiguration
  /** Which project the configuration came from, for a panel that fetches a workspace file. */
  source: { workspaceId: string; vastPath: string }
  /** URL for a workspace-relative path, for `contribution.files` entries. */
  fileUrl: (relativePath: string) => string
}
