// What the configured share holds that this deployment does not.
//
// The share is a separate system, and what is on it is deliberately NOT treated as a
// subset of what the service has: a campaign can be deleted here while its archive stays
// up there. Those archives are the ones worth a panel -- a campaign that *is* here is
// reachable from its own card's download menu, so listing it again would be noise. An
// archive with no campaign here is the ordinary case import exists for, not an anomaly.
//
// Nothing is cached onto a campaign to make this work. The listing is one request per
// page, shared with the cards through its react-query key, and the share answers for
// itself -- a "has a share copy" field on a campaign would be a copy of another system's
// state, wrong the first time somebody deleted an archive out of band.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import Collapse from '@mui/material/Collapse'
import IconButton from '@mui/material/IconButton'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import CloudDownloadRoundedIcon from '@mui/icons-material/CloudDownloadRounded'
import LinkRoundedIcon from '@mui/icons-material/LinkRounded'
import KeyboardArrowDownRoundedIcon from '@mui/icons-material/KeyboardArrowDownRounded'
import KeyboardArrowUpRoundedIcon from '@mui/icons-material/KeyboardArrowUpRounded'
import { robovast, type ShareArchive } from '@/lib/robovastClient'
import { formatBytes } from '@/lib/format'

export function ShareInventory({ presentIds }: { presentIds: Set<string> }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  // Archives this panel has dispatched an import for. An import registers its campaign
  // before any bytes move, so without this the row you clicked drops out of `elsewhere`
  // about a second later and every row below it slides up -- under the pointer, while the
  // list is still being read. Two adjacent ids and one further click is all it takes to
  // import an archive nobody chose, and the panel's only account of it is a campaign card
  // under the other id. Keeping the row put costs nothing and removes the whole class.
  const [started, setStarted] = useState<Set<string>>(() => new Set())

  const listing = useQuery({
    queryKey: ['shareArchives'],
    queryFn: () => robovast.listShareArchives(),
    staleTime: 60_000,
    retry: false,
  })

  const importing = useMutation({
    mutationFn: (a: ShareArchive) => robovast.importFromShare(a.campaign_id),
    onMutate: (a: ShareArchive) => {
      setStarted((prev) => new Set(prev).add(a.campaign_id))
    },
    onSuccess: () => {
      // The campaign appears in the live list on its own (the service pushes the list on
      // every change, and it is registered at phase `importing` before any bytes move),
      // so there is nothing to refetch but this panel: the archive is no longer "not here".
      void qc.invalidateQueries({ queryKey: ['shareArchives'] })
    },
  })

  // No share configured is not an error and not an empty share -- there is simply nothing
  // to show, so the panel stays out of the way entirely. Same for an unreachable one:
  // the campaign list is the point of this page and must not be crowded by a failure to
  // reach a secondary system.
  if (!listing.data?.configured || listing.isError) return null

  const elsewhere = listing.data.archives.filter(
    (a) => !presentIds.has(a.campaign_id) || started.has(a.campaign_id),
  )
  if (!elsewhere.length) return null

  return (
    <Paper variant="outlined" sx={{ p: 1.5 }}>
      <Stack direction="row" alignItems="center" spacing={1}>
        <CloudDownloadRoundedIcon fontSize="small" color="action" />
        <Typography variant="subtitle2" sx={{ flexGrow: 1 }}>
          On the {listing.data.share_type} share, not here
          <Typography component="span" variant="caption" color="text.secondary" sx={{ ml: 1 }}>
            {elsewhere.length} archive{elsewhere.length === 1 ? '' : 's'}
          </Typography>
        </Typography>
        <IconButton size="small" onClick={() => setOpen((v) => !v)} aria-label="toggle">
          {open ? <KeyboardArrowUpRoundedIcon /> : <KeyboardArrowDownRoundedIcon />}
        </IconButton>
      </Stack>

      <Collapse in={open}>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
          Importing has the service fetch the archive, so nothing comes through this browser.
          A raw archive is postprocessed once it lands. The import continues in the
          background, so how it ends is reported on the campaign&apos;s own card, not here.
        </Typography>

        {importing.isError ? (
          <Alert severity="error" variant="outlined" sx={{ mt: 1 }}>
            {String((importing.error as Error).message)}
          </Alert>
        ) : null}

        <Stack spacing={0.5} sx={{ mt: 1 }}>
          {elsewhere.map((a) => (
            <Stack
              key={a.object_name}
              direction="row"
              alignItems="center"
              spacing={1}
              sx={{ py: 0.25 }}
            >
              <Typography variant="body2" sx={{ flexGrow: 1, fontFamily: 'monospace' }}>
                {a.campaign_id}
              </Typography>
              <Chip size="small" variant="outlined" label={a.variant} />
              <Typography variant="caption" color="text.secondary">
                {a.size >= 0 ? formatBytes(a.size) : 'unknown size'}
              </Typography>
              {/* Absent for a provider with no openable link -- sftp never has one. */}
              {a.url ? (
                <Tooltip title="Copy share link">
                  <IconButton
                    size="small"
                    onClick={() => void navigator.clipboard?.writeText(a.url as string)}
                    aria-label={`copy link for ${a.campaign_id}`}
                  >
                    <LinkRoundedIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
              ) : null}
              {/* Only this row's button is disabled once it has been sent. Disabling every
                  button on any pending mutation made the whole panel look broken for the
                  moment the request was in flight, and said nothing about which archive
                  had gone. */}
              <Button
                size="small"
                variant="outlined"
                disabled={started.has(a.campaign_id)}
                onClick={() => importing.mutate(a)}
              >
                {started.has(a.campaign_id) ? 'Importing…' : 'Import'}
              </Button>
            </Stack>
          ))}
        </Stack>
      </Collapse>
    </Paper>
  )
}
