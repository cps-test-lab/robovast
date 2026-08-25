// The package's public surface. Both consumers import from '@robovast/panel-kit' only, never from a
// file inside it, so what is shared stays visible in one place.

export type { ClockSnapshot, ClockSource } from './clock'
export { PlaybackClock, useClock } from './clock'

export type { DataProvider, DataRow, SeriesOptions, SeriesPage } from './dataProvider'

export type { PanelBuiltins, PanelProps, PanelSpec } from './panel'

export type {
  ConfigPanelProps,
  ConfigViewContribution,
  ResolvedConfiguration,
  SceneMarker,
  VariationPreview,
} from './configPanel'

export type { DeclaredMarker } from './declaredMarkers'
export { declaredMarkers } from './declaredMarkers'

export type { BindingSource } from './bindings'
export { resolveBinding, resolveStringBinding } from './bindings'

export type { YamlScalar } from './flatYaml'
export { numberSequence, parseFlatYaml } from './flatYaml'

export type { MapYaml } from './mapYaml'
export { parseMapYaml } from './mapYaml'

export { useCanvasClock } from './useCanvasClock'

export { lastAtOrBefore, nearestIndex } from './timeIndex'

export type {
  FrameContext,
  FrameValidity,
  KeyframePumpOptions,
  ValidityOptions,
} from './keyframes'
export { DEFAULT_MIN_INTERVAL_MS, frameValidity, STALE_PERIODS, useKeyframePump } from './keyframes'
