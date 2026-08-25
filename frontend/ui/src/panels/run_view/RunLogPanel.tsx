// RunLogPanel (type `log`): everything the run said, following the playback cursor.
//
// A thin wrapper by design. The view, the filters and the loader live in
// `components/runLog/` because the Explorer's Log tab renders the same thing over a wider
// scope; the one thing only this host can supply is the clock. So the panel's whole job is:
// read the clock, hand the cursor down, and let a click in the log seek it back.
//
// Named RunLogPanel, not LogPanel: `components/StatusView.tsx` already has a LogPanel, and
// that one is the *live* campaign/job log streamed over SSE while a run is executing. Two
// different questions -- what is happening now, versus what happened at t=41.2 s.
//
// Bindings (vast visualization.panels) -- all optional, since the table's name and columns are
// fixed by the merge that writes it:
//   max_rows: cap on the initial load (default 20000; hitting it is reported in the footer)
//   severities: push a severity floor into the query, e.g. [warn, error]

import { useMemo } from 'react'
import { registerPanel } from '@/lib/panels/registry'
import { RunLogView } from '@/components/runLog/RunLogView'
import { useRunLog } from '@/components/runLog/useRunLog'
import { useClock, type PanelProps } from '@robovast/panel-kit'

function RunLogPanel({ spec, clock, data }: PanelProps) {
  // `hideShutdown` too, not just the cursor: the run view has one shutdown state, reached from
  // its header, and the log is one of the two things it governs. Reading it here rather than
  // owning a copy is what keeps the log and the timeline agreeing about where the run ended --
  // and is why this panel's filter bar shows no shutdown button of its own.
  const { t, hideShutdown } = useClock(clock)
  const maxRows = spec.config.max_rows as number | undefined
  const severities = spec.config.severities as string[] | undefined

  const runId = useMemo(() => {
    const n = Number(data.runId)
    return Number.isFinite(n) ? n : undefined
  }, [data.runId])

  const log = useRunLog({
    campaignId: data.campaignId,
    configName: data.configName,
    runId,
    severities,
    maxRows,
  })

  return (
    <RunLogView
      data={log.data}
      isPending={log.isPending}
      error={log.error}
      cursor={t}
      hideShutdown={hideShutdown}
      onSeek={(simTime) => clock.seek(simTime)}
    />
  )
}

registerPanel({
  manifest: {
    type: 'log',
    label: 'Run log',
    // Bottom-centre between the two corner columns, and collapsed: the log is what you reach
    // for when something looks wrong, not a permanent layer over the replay. A declared width
    // is also what makes `bottom-center` float above the playback bar instead of docking.
    defaultPosition: { anchor: 'bottom-center', width: '60%', height: 200 },
    resizable: true,
    minimizable: true,
  },
  component: RunLogPanel,
})

export default RunLogPanel
