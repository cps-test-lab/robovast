// The campaign list's multi-campaign delete: the bar that appears in selection mode, and the one
// dialog that confirms the picked ids and then reports what happened to each. Kept out of
// Monitor.tsx so the list only has to hold the selection and hand it here.

import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import CircularProgress from '@mui/material/CircularProgress'
import Dialog from '@mui/material/Dialog'
import DialogActions from '@mui/material/DialogActions'
import DialogContent from '@mui/material/DialogContent'
import DialogContentText from '@mui/material/DialogContentText'
import DialogTitle from '@mui/material/DialogTitle'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import DeleteOutlineRoundedIcon from '@mui/icons-material/DeleteOutlineRounded'
import { robovast, type CampaignDeletion, type CampaignSummary } from '@/lib/robovastClient'
import {
  OUTCOME_LABEL,
  deletionSummary,
  isSelectable,
  selectedInListOrder,
} from '@/lib/campaignSelection'
import { ErrorText } from '@/components/StatusView'

export function BulkDeleteBar({
  selected,
  shown,
  all,
  onChange,
  onDone,
}: {
  selected: ReadonlySet<string>
  /** The campaigns the list shows now, in its order — what "select all" means. */
  shown: readonly CampaignSummary[]
  /** Every campaign listed, filter or not: a picked campaign the search now hides is still sent. */
  all: readonly CampaignSummary[]
  onChange: (next: ReadonlySet<string>) => void
  /** Leave selection mode. */
  onDone: () => void
}) {
  const [asked, setAsked] = useState<string[] | null>(null)
  const selectable = shown.filter(isSelectable).map((c) => c.campaign_id)
  const allPicked = selectable.length > 0 && selectable.every((cid) => selected.has(cid))
  const somePicked = selectable.some((cid) => selected.has(cid))

  return (
    <Paper variant="outlined" sx={{ px: 2, py: 0.75 }}>
      <Stack direction="row" alignItems="center" spacing={1}>
        <Checkbox
          size="small"
          checked={allPicked}
          indeterminate={somePicked && !allPicked}
          disabled={!selectable.length}
          inputProps={{ 'aria-label': 'select every finished campaign shown' }}
          onChange={() => {
            const next = new Set(selected)
            for (const cid of selectable) {
              if (allPicked) next.delete(cid)
              else next.add(cid)
            }
            onChange(next)
          }}
          sx={{ ml: -1 }}
        />
        <Typography variant="body2" color="text.secondary" sx={{ flexGrow: 1 }}>
          {selected.size
            ? `${selected.size} selected`
            : 'Select finished campaigns to delete — a running one cannot be picked.'}
        </Typography>
        <Button
          size="small"
          color="error"
          variant="contained"
          startIcon={<DeleteOutlineRoundedIcon fontSize="small" />}
          disabled={!selected.size}
          onClick={() => setAsked(selectedInListOrder(selected, all))}
        >
          Delete {selected.size || ''} selected
        </Button>
        <Button size="small" color="inherit" onClick={onDone}>
          Done
        </Button>
      </Stack>
      {asked ? (
        <BulkDeleteDialog
          ids={asked}
          onClose={(deleted) => {
            setAsked(null)
            if (deleted.length) {
              const next = new Set(selected)
              for (const cid of deleted) next.delete(cid)
              onChange(next)
            }
          }}
        />
      ) : null}
    </Paper>
  )
}

// One dialog for the whole gesture: it lists what will go, and after the delete it lists what
// did, id by id, in place — so the reader checks the answer against the same list they approved.
function BulkDeleteDialog({ ids, onClose }: {
  ids: string[]
  /** Called with the ids that are gone, to drop them from the selection. */
  onClose: (deleted: string[]) => void
}) {
  const qc = useQueryClient()
  const del = useMutation({
    mutationFn: () => robovast.deleteCampaigns(ids),
    onSettled: () => qc.invalidateQueries({ queryKey: ['campaigns'] }),
  })
  const results: CampaignDeletion[] | undefined = del.data?.results
  const summary = results ? deletionSummary(results) : null
  const close = () => {
    if (del.isPending) return
    onClose(results?.filter((r) => r.ok).map((r) => r.campaign_id) ?? [])
  }

  return (
    <Dialog open onClose={close} maxWidth="sm" fullWidth>
      <DialogTitle>
        {summary ? summary.text : `Delete ${ids.length} campaign${ids.length === 1 ? '' : 's'}?`}
      </DialogTitle>
      <DialogContent>
        {!results ? (
          <DialogContentText component="div">
            Permanently delete these campaigns and all their data. This cannot be undone. Any copy
            on the external share is left untouched.
          </DialogContentText>
        ) : null}
        {del.isError ? (
          // The call itself failed, so no id has an outcome to show: say so rather than draw the
          // list as though each were still pending.
          <DialogContentText component="div" color="error" sx={{ mt: 1 }}>
            The delete request failed; the service may have deleted none, some or all of them.
            <ErrorText>{(del.error as Error).message}</ErrorText>
          </DialogContentText>
        ) : null}
        <Stack component="ul" spacing={0.5} sx={{ pl: 0, mt: 1.5, listStyle: 'none' }}>
          {(results ?? ids.map((cid) => ({ campaign_id: cid }) as Partial<CampaignDeletion>))
            .map((r) => (
              <li key={r.campaign_id}>
                <Stack direction="row" spacing={1} alignItems="baseline">
                  <Typography variant="body2" component="code" sx={{ fontFamily: 'monospace' }}>
                    {r.campaign_id}
                  </Typography>
                  {r.outcome ? (
                    <Typography
                      variant="caption"
                      color={r.ok ? 'success.main' : 'error.main'}
                      title={r.message}
                    >
                      {OUTCOME_LABEL[r.outcome]}
                    </Typography>
                  ) : null}
                </Stack>
                {/* The service's own sentence only where it adds something: a partial delete
                    says what it had to leave, which is the reader's next step. */}
                {r.outcome === 'partial' ? (
                  <Typography variant="caption" color="text.secondary" component="div">
                    {r.message}
                  </Typography>
                ) : null}
              </li>
            ))}
        </Stack>
      </DialogContent>
      <DialogActions>
        {results || del.isError ? (
          <Button onClick={close} variant="contained">
            Close
          </Button>
        ) : (
          <>
            <Button onClick={close} color="inherit" disabled={del.isPending}>
              Cancel
            </Button>
            <Button
              onClick={() => del.mutate()}
              variant="contained"
              color="error"
              disabled={del.isPending}
              startIcon={del.isPending ? <CircularProgress size={16} /> : undefined}
            >
              Delete {ids.length}
            </Button>
          </>
        )}
      </DialogActions>
    </Dialog>
  )
}
