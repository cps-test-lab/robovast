// Bringing in a campaign this deployment does not have, off the configured share.
//
// The share is a separate system, and what is on it is deliberately NOT treated as a subset
// of what the service has: a campaign can be deleted here while its archive stays up there.
// Nothing about it is cached onto a campaign either — a "has a share copy" field would be a
// copy of another system's state, wrong the first time somebody deleted an archive out of
// band. The listing is one request per page, shared with the cards through its react-query
// key, and the share answers for itself.
//
// This was a panel under the campaign list, which put it off the bottom of the page on any
// deployment with a long list — collapsed, at that. A dialog off the import menu is reachable
// in one click however long the list is, and it can afford a search box, which a share
// holding hundreds of archives needs.
//
// Everything IS listed, including campaigns already here, which the panel filtered out. That
// is for the deep link's sake: somebody handed a link to a campaign they already have must
// be told so, not shown an empty dialog.

import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import CircularProgress from '@mui/material/CircularProgress'
import Dialog from '@mui/material/Dialog'
import DialogActions from '@mui/material/DialogActions'
import DialogContent from '@mui/material/DialogContent'
import DialogTitle from '@mui/material/DialogTitle'
import IconButton from '@mui/material/IconButton'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import CheckRoundedIcon from '@mui/icons-material/CheckRounded'
import LinkRoundedIcon from '@mui/icons-material/LinkRounded'

import { robovast } from '@/lib/robovastClient'
import { formatBytes } from '@/lib/format'
import { shareImportLink } from '@/lib/nav'
import { matchRows, shareRows, type ShareCampaignRow } from '@/lib/shareArchives'

export function ShareImportDialog({
  open,
  onClose,
  initialSearch,
  presentIds,
}: {
  open: boolean
  onClose: () => void
  /** Seed from a deep link's `?import=`; empty when opened from the menu. */
  initialSearch: string
  presentIds: Set<string>
}) {
  const [search, setSearch] = useState(initialSearch)

  const listing = useQuery({
    queryKey: ['shareArchives'],
    queryFn: () => robovast.listShareArchives(),
    staleTime: 60_000,
    retry: false,
  })

  // The import is a TRACKED background operation: the POST returns as soon as the work is
  // under way and the campaign appears in the list at phase `importing`. So a success has
  // nothing left to show here and closes; a refusal — not a campaign archive, an id already
  // here — creates no campaign row at all, and would vanish silently if this closed too.
  const importing = useMutation({
    mutationFn: (row: ShareCampaignRow) => robovast.importFromShare(row.archive),
    onSuccess: onClose,
  })

  const rows = useMemo(
    () => shareRows(listing.data?.archives ?? [], presentIds),
    [listing.data, presentIds],
  )
  const shown = useMemo(() => matchRows(rows, search), [rows, search])

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>
        Import from the {listing.data?.share_type || 'configured'} share
      </DialogTitle>
      <DialogContent dividers>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.5 }}>
          The service fetches the archive, so nothing comes through this browser. A raw
          archive is postprocessed once it lands, so that campaign runs on into a second
          phase. The import continues in the background and how it ends is reported on the
          campaign&apos;s own card, not here.
        </Typography>

        <TextField
          autoFocus
          fullWidth
          size="small"
          label="Search campaigns"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />

        {importing.isError ? (
          <Alert severity="error" variant="outlined" sx={{ mt: 1.5 }}>
            {String((importing.error as Error).message)}
          </Alert>
        ) : null}

        <Body
          loading={listing.isLoading}
          failed={listing.isError}
          configured={!!listing.data?.configured}
          rows={rows}
          shown={shown}
          search={search}
          pending={importing.isPending ? (importing.variables?.campaignId ?? null) : null}
          onImport={(row) => importing.mutate(row)}
        />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  )
}

/** Either the rows, or the one thing standing in their way.
 *
 *  A `return null` would do on a panel nobody asked for. This dialog is opened deliberately —
 *  from the menu, or by following somebody's link — so it owes an answer even when the answer
 *  is that there is nothing. The
 *  unconfigured case is reachable despite the menu disabling its entry, because a link opens
 *  this regardless of the menu. */
function Body({
  loading,
  failed,
  configured,
  rows,
  shown,
  search,
  pending,
  onImport,
}: {
  loading: boolean
  failed: boolean
  configured: boolean
  rows: ShareCampaignRow[]
  shown: ShareCampaignRow[]
  search: string
  pending: string | null
  onImport: (row: ShareCampaignRow) => void
}) {
  if (loading) {
    return <CircularProgress size={24} sx={{ mt: 2 }} />
  }
  if (failed) {
    return (
      <Alert severity="error" variant="outlined" sx={{ mt: 2 }}>
        Could not read the share. It is configured, so this is the share itself or the way to
        it — the campaigns already here are unaffected.
      </Alert>
    )
  }
  if (!configured) {
    return (
      <Alert severity="info" variant="outlined" sx={{ mt: 2 }}>
        This deployment has no share configured, so there is nothing to import from. If you
        followed a link to a campaign, whoever sent it is on a different deployment.
      </Alert>
    )
  }
  if (!rows.length) {
    return (
      <Alert severity="info" variant="outlined" sx={{ mt: 2 }}>
        The share holds no campaign archives.
      </Alert>
    )
  }

  return (
    <>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
        {search ? `showing ${shown.length} of ${rows.length}` : `${rows.length} campaigns`}
      </Typography>
      {!shown.length ? (
        <Alert severity="info" variant="outlined" sx={{ mt: 1 }}>
          No campaign on the share matches “{search}”.
        </Alert>
      ) : (
        <Stack spacing={0.5} sx={{ mt: 1 }}>
          {shown.map((row) => (
            <Row
              key={row.campaignId}
              row={row}
              pending={pending === row.campaignId}
              onImport={() => onImport(row)}
            />
          ))}
        </Stack>
      )}
    </>
  )
}

function Row({
  row,
  pending,
  onImport,
}: {
  row: ShareCampaignRow
  pending: boolean
  onImport: () => void
}) {
  return (
    <Stack direction="row" alignItems="center" spacing={1} sx={{ py: 0.25 }}>
      <Typography variant="body2" sx={{ flexGrow: 1, fontFamily: 'monospace' }}>
        {row.campaignId}
      </Typography>
      {/* Which of the campaign's archives this row means, and how big THAT one is -- not the
          campaign, which may be on the share twice at two sizes. */}
      <Chip size="small" variant="outlined" label={row.variant} />
      <Typography variant="caption" color="text.secondary">
        {row.size >= 0 ? formatBytes(row.size) : 'unknown size'}
      </Typography>
      {row.present ? (
        <Typography variant="caption" color="text.secondary">
          already here
        </Typography>
      ) : null}
      {/* Offered even for a campaign already here: the link is about the archive on the
          share, and stays worth sending to whoever has not got it yet. */}
      <CopyLinkButton campaignId={row.campaignId} />
      {/* Disabled rather than dropped for a campaign already here. Dropping it would let
          every such row's copy button slide into the space, so a column read down the list
          would not line up -- and the disabled control is itself the statement that this is
          the row where importing is not the thing to do. */}
      <Button
        size="small"
        variant="outlined"
        disabled={row.present || pending}
        onClick={onImport}
      >
        {pending ? 'Importing…' : 'Import'}
      </Button>
    </Stack>
  )
}

/** Copy the deep link that opens this dialog on this campaign.
 *
 *  There is no snackbar anywhere in this app, and one import dialog is not the place to
 *  introduce toast infrastructure — so the acknowledgement is the icon itself, briefly. A
 *  copy button that looks identical before and after is one people press twice. */
function CopyLinkButton({ campaignId }: { campaignId: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <Tooltip title={copied ? 'Copied' : 'Copy a link that opens this import'}>
      <IconButton
        size="small"
        aria-label={`copy import link for ${campaignId}`}
        onClick={() => {
          void navigator.clipboard?.writeText(shareImportLink(campaignId))
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        }}
      >
        {copied ? (
          <CheckRoundedIcon fontSize="small" color="success" />
        ) : (
          <LinkRoundedIcon fontSize="small" />
        )}
      </IconButton>
    </Tooltip>
  )
}
