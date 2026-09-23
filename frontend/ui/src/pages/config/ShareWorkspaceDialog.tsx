// Taking a workspace off the configured share, as a new workspace here.
//
// The workspace half of `pages/ShareImportDialog` and deliberately a separate dialog: that
// one imports a campaign, which is restored under its own id and may already be here, and
// this one always CREATES — an archive carries project files and no identity, so there is
// nothing to replace, no `force`, and no "already here" to report. One dialog serving both
// would have to branch on every one of those.
//
// Synchronous, unlike a campaign's import: the service fetches the archive and unpacks it
// within the request, so a success has a workspace to select and a failure has its reason
// here rather than on a row somewhere else.

import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import Dialog from '@mui/material/Dialog'
import DialogActions from '@mui/material/DialogActions'
import DialogContent from '@mui/material/DialogContent'
import DialogTitle from '@mui/material/DialogTitle'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'

import { robovast } from '@/lib/robovastClient'
import { formatBytes } from '@/lib/format'
import {
  matchWorkspaceRows,
  workspaceRows,
  type ShareWorkspaceRow,
} from '@/lib/shareArchives'

export function ShareWorkspaceDialog({
  open,
  onClose,
  onImported,
}: {
  open: boolean
  onClose: () => void
  /** The new workspace's id, so the page can select what was just brought in. */
  onImported: (workspaceId: string) => void
}) {
  const [search, setSearch] = useState('')

  const listing = useQuery({
    queryKey: ['shareArchives'],
    queryFn: () => robovast.listShareArchives(),
    staleTime: 60_000,
    retry: false,
    enabled: open,
  })

  const importing = useMutation({
    mutationFn: (row: ShareWorkspaceRow) => robovast.createWorkspace('', '', row.archive),
    onSuccess: (ws) => {
      onImported(ws.workspace_id)
      onClose()
    },
  })

  const rows = useMemo(
    () => workspaceRows(listing.data?.workspaces ?? []),
    [listing.data],
  )
  const shown = useMemo(() => matchWorkspaceRows(rows, search), [rows, search])

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>
        Import a workspace from the {listing.data?.share_type || 'configured'} share
      </DialogTitle>
      <DialogContent dividers>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.5 }}>
          The service fetches the archive, so nothing comes through this browser. Each import
          creates a new workspace named after the archive; nothing here is replaced.
        </Typography>

        <TextField
          autoFocus
          fullWidth
          size="small"
          label="Search workspaces"
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
          pending={importing.isPending ? (importing.variables?.slug ?? null) : null}
          onImport={(row) => importing.mutate(row)}
        />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  )
}

/** Either the rows, or the one thing standing in their way. */
function Body({
  loading,
  failed,
  configured,
  rows,
  shown,
  pending,
  onImport,
}: {
  loading: boolean
  failed: boolean
  configured: boolean
  rows: ShareWorkspaceRow[]
  shown: ShareWorkspaceRow[]
  pending: string | null
  onImport: (row: ShareWorkspaceRow) => void
}) {
  if (loading) {
    return <CircularProgress size={24} sx={{ mt: 2 }} />
  }
  if (failed) {
    return (
      <Alert severity="error" variant="outlined" sx={{ mt: 2 }}>
        Could not read the share. It is configured, so this is the share itself or the way to
        it — the workspaces already here are unaffected.
      </Alert>
    )
  }
  if (!configured) {
    return (
      <Alert severity="info" variant="outlined" sx={{ mt: 2 }}>
        This deployment has no share configured, so there is nothing to import from.
      </Alert>
    )
  }
  if (!rows.length) {
    return (
      <Alert severity="info" variant="outlined" sx={{ mt: 2 }}>
        The share holds no workspace archives. Publish one with the Export button, or with
        <code> vast share export --workspace</code>.
      </Alert>
    )
  }
  if (!shown.length) {
    return (
      <Alert severity="info" variant="outlined" sx={{ mt: 2 }}>
        No workspace on the share matches that search. It holds {rows.length}.
      </Alert>
    )
  }
  return (
    <Stack sx={{ mt: 1.5 }}>
      {shown.map((row) => (
        <Stack
          key={row.archive}
          direction="row"
          spacing={2}
          alignItems="center"
          sx={{ py: 0.75, borderBottom: 1, borderColor: 'divider' }}
        >
          <Typography sx={{ fontFamily: 'monospace', flexGrow: 1 }}>{row.slug}</Typography>
          <Typography variant="caption" color="text.secondary">
            {row.size >= 0 ? formatBytes(row.size) : 'unknown size'}
          </Typography>
          <Button
            size="small"
            variant="outlined"
            disabled={pending !== null}
            onClick={() => onImport(row)}
          >
            {pending === row.slug ? 'Importing…' : 'Import'}
          </Button>
        </Stack>
      ))}
    </Stack>
  )
}
