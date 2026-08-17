// ColumnHost lays out the Config tab's third column: the panels a .vast declares under
// `visualization.config.panels`, stacked top to bottom in declaration order.
//
// Deliberately not PanelHost. That host exists to place free-floating panels against fourteen
// anchors and let them be dragged and resized; this is a fixed column whose order and sizes are the
// campaign author's, so the whole layout is `flex-direction: column` and one `flex` per member.
// What the two share -- the panel frame, and the "remote / registered / explicit error" dispatch --
// is imported rather than repeated.
//
// A member's height is pixels, a percentage of the column, or absent. Absent means "take what the
// ones above left over", which is why only the last panel may omit it (enforced in the .vast schema,
// so the message names the panel rather than the layout silently overlapping two).

import Box from '@mui/material/Box'
import type { ConfigPanelProps } from '@robovast/panel-kit'
import { useRemoteComponent } from '@/lib/remote'
import { PanelChrome, UnknownPanel } from './PanelHost'
import { getConfigPanel } from './registry'
import type { ConfigPanelSpec } from './types'

/** Matches PanelHost's gap, so the two views are visually of a piece. */
const PANEL_GAP = 8

/** Loads a Module-Federation remote config panel and mounts it with the same props a built-in
 *  gets, so where a panel's code lives stays invisible to the .vast. */
function RemoteConfigPanel(props: ConfigPanelProps & { spec: ConfigPanelSpec }) {
  const { Comp, err } = useRemoteComponent<ConfigPanelProps>(props.spec.remote!)
  if (err) {
    return (
      <Box sx={{ p: 2, color: 'error.main', fontSize: 13 }}>
        Panel “{props.spec.remote!.name}” failed to load ({err}). Check the panel bundle / its
        providing plugin.
      </Box>
    )
  }
  if (!Comp) {
    return (
      <Box sx={{ p: 2, color: 'text.secondary', fontSize: 13 }}>
        loading panel “{props.spec.remote!.name}”…
      </Box>
    )
  }
  return <Comp {...props} />
}

function ConfigPanelFrame({ spec, ...rest }: ConfigPanelProps & { spec: ConfigPanelSpec }) {
  const plugin = getConfigPanel(spec.type)
  const body = spec.remote ? (
    <RemoteConfigPanel spec={spec} {...rest} />
  ) : plugin ? (
    <plugin.component spec={spec} {...rest} />
  ) : (
    <UnknownPanel type={spec.type} where="visualization.config.panels" />
  )
  return (
    <PanelChrome header={{ title: spec.title ?? plugin?.manifest.label ?? spec.type }}>
      {body}
    </PanelChrome>
  )
}

/** A member's flex sizing. A declared height is fixed (`flex: 0 0 <h>`); an undeclared one grows
 *  into whatever is left. `minHeight: 0` on both, or a panel with a scrolling body refuses to
 *  shrink below its content and pushes the ones after it off the column. */
function flexFor(height: number | string | undefined) {
  if (height == null) return { flex: '1 1 0', minHeight: 0 }
  const basis = typeof height === 'number' ? `${height}px` : height
  return { flex: `0 0 ${basis}`, minHeight: 0 }
}

export function ColumnHost({
  panels,
  ...shared
}: {
  panels: ConfigPanelSpec[]
} & Omit<ConfigPanelProps, 'spec'>) {
  const visible = panels.filter((p) => !p.hidden)
  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        gap: `${PANEL_GAP}px`,
        height: '100%',
        minHeight: 0,
      }}
    >
      {visible.map((spec, i) => (
        // Keyed on the spec so only a panel whose declaration actually changed remounts: editing
        // one panel's bindings must not reset another's view (a 3D camera, a map's pan/zoom).
        <Box key={`${i}:${JSON.stringify(spec)}`} sx={{ ...flexFor(spec.height), display: 'flex' }}>
          <ConfigPanelFrame spec={spec} {...shared} />
        </Box>
      ))}
    </Box>
  )
}
