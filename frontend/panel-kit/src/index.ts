// The package's public surface. Both consumers import from '@robovast/panel-kit' only, never from a
// file inside it, so what is shared stays visible in one place.

export type { ClockSnapshot, ClockSource } from './clock'
export { PlaybackClock, useClock } from './clock'

export type { DataProvider, DataRow, SeriesOptions } from './dataProvider'

export type { PanelBuiltins, PanelProps, PanelSpec } from './panel'

export { useCanvasClock } from './useCanvasClock'

export { lastAtOrBefore, nearestIndex } from './timeIndex'

export type {
  FrameContext,
  FrameValidity,
  KeyframePumpOptions,
  ValidityOptions,
} from './keyframes'
export { DEFAULT_MIN_INTERVAL_MS, frameValidity, STALE_PERIODS, useKeyframePump } from './keyframes'
