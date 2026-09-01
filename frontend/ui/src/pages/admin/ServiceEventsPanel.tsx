import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import Chip from '@mui/material/Chip'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'

import { robovast, type ServiceEvent } from '@/lib/robovastClient'
import { eventTone, hasMore, newestFirst } from '@/lib/serviceEvents'

// What the service did, kept where a restart cannot take it.
//
// Distinct from the Service log above it, and the distinction is the point: that log is this
// process's recent stderr and dies with the pod, and the usage samples say the same of
// themselves. These outlive both. What they carry today is REFUSALS -- a campaign's failure is
// on its card and in its outcome.json, but an action the service would not do was composed in
// the request that refused it and shown once.
//
// Newest first here, though the route serves oldest-first from a cursor: a reader resuming a
// position wants what came after it, and a person opening a panel wants what just happened.

function when(at: number): string {
  return new Date(at * 1000).toLocaleString()
}

function EventRow({ event }: { event: ServiceEvent }) {
  const tone = eventTone(event.severity)
  return (
    <Box sx={{ py: 0.75, borderBottom: 1, borderColor: 'divider' }}>
      <Stack direction="row" spacing={1} alignItems="baseline" sx={{ flexWrap: 'wrap' }}>
        <Chip size="small" label={event.kind} color={tone} variant="outlined" />
        <Typography variant="caption" color="text.secondary">{when(event.at)}</Typography>
        {event.actor ? (
          <Typography variant="caption" color="text.secondary">· {event.actor}</Typography>
        ) : null}
        {event.subject_id ? (
          <Typography variant="caption" color="text.secondary">
            · {event.subject_type || 'subject'} {event.subject_id}
          </Typography>
        ) : null}
        {typeof event.payload?.status === 'number' ? (
          <Typography variant="caption" color="text.secondary">
            · HTTP {String(event.payload.status)}
          </Typography>
        ) : null}
      </Stack>
      {event.message ? (
        // The service's own words, in the same monospace treatment a failure gets everywhere
        // else here: it is usually a sentence worth copying rather than prose to skim.
        <Box
          component="span"
          sx={{
            display: 'block', mt: 0.25, fontFamily: 'monospace', fontSize: '0.75rem',
            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          }}
        >
          {event.message}
        </Box>
      ) : null}
    </Box>
  )
}

export function ServiceEventsPanel() {
  const [limit, setLimit] = useState(50)
  const events = useQuery({
    queryKey: ['service-events', limit],
    queryFn: () => robovast.serviceEvents(0, limit),
  })

  if (events.isPending) return <CircularProgress size={20} />
  if (events.isError) {
    return <Alert severity="error">{(events.error as Error).message}</Alert>
  }

  const rows = newestFirst(events.data?.events ?? [])
  if (rows.length === 0) {
    // Not an error, and worth saying which: an empty record means nothing has been refused,
    // not that nothing is being kept.
    return (
      <Typography variant="body2" color="text.secondary">
        Nothing recorded yet. This fills as the service refuses things — it is a record of what
        did not happen, not a request log.
      </Typography>
    )
  }

  return (
    <Stack spacing={0}>
      {rows.map((e) => <EventRow key={e.seq} event={e} />)}
      {/* Only offered when the page is full: a shorter answer is the whole record. */}
      {hasMore(rows.length, limit) ? (
        <Button size="small" sx={{ alignSelf: 'flex-start', mt: 1 }}
                onClick={() => setLimit((n) => n + 200)}>
          Show more
        </Button>
      ) : null}
    </Stack>
  )
}
