import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import Dialog from '@mui/material/Dialog'
import DialogActions from '@mui/material/DialogActions'
import DialogContent from '@mui/material/DialogContent'
import DialogTitle from '@mui/material/DialogTitle'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import Editor from '@monaco-editor/react'
import { robovast } from '@/lib/robovastClient'
import { ErrorText } from '@/components/StatusView'

// The 'Retrigger postprocessing' modal. A campaign is self-contained — it carries the `.vast`
// that ran — so the only way to compute different metrics used to be hand-editing that file.
// Here the user adapts the `results_processing.postprocessing` block in Monaco (same editor as
// the run-view 'edit visualization' dropdown) and reruns. If the text changed, it is first saved
// as a new `.vast` override revision (validated server-side; the immutable `_config/` snapshot is
// never touched), then postprocessing reruns against the effective config.
export function PostprocessingDialog({
  campaignId,
  open,
  onClose,
}: {
  campaignId: string
  open: boolean
  onClose: () => void
}) {
  const qc = useQueryClient()

  const src = useQuery({
    queryKey: ['postprocessing-source', campaignId],
    queryFn: () => robovast.getPostprocessingSource(campaignId),
    enabled: open,
    retry: false,
  })

  const [text, setText] = useState<string | null>(null)
  // Load the fetched source into the buffer (and reset it on reopen / after a save writes a new rev).
  useEffect(() => {
    if (src.data) setText(src.data.content)
  }, [src.data])

  const original = src.data?.content ?? ''
  const changed = text != null && text !== original

  // Save (only if edited) the overwritten config, then *dispatch* the rerun. The service now
  // runs postprocessing in the background and returns at once, so this mutation is quick — the
  // long-running work is watched in the campaign view, not here.
  const saveAndRerun = useMutation({
    mutationFn: async () => {
      if (changed) await robovast.updatePostprocessingSource(campaignId, text ?? '')
      return robovast.runPostprocessing(campaignId)
    },
    onSuccess: (result) => {
      // Not ok = the busy guard (an operation is already running): keep the dialog open and
      // show why. On a successful dispatch, close so the campaign view can show progress —
      // invalidating the row's status query restarts its polling (it had stopped once the
      // campaign was terminal), so it picks up the new `postprocessing` phase.
      if (!result.ok) return
      qc.invalidateQueries({ queryKey: ['status', campaignId] })
      qc.invalidateQueries({ queryKey: ['campaigns'] })
      qc.invalidateQueries({ queryKey: ['postprocessing-source', campaignId] })
      onClose()
    },
  })

  const busy = saveAndRerun.isPending
  const result = saveAndRerun.data

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} maxWidth="md" fullWidth>
      <DialogTitle>
        Retrigger postprocessing
        <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
          {campaignId}
        </Typography>
      </DialogTitle>
      <DialogContent>
        <Stack spacing={1.5} sx={{ pt: 1 }}>
          <Typography variant="body2" color="text.secondary">
            Adapt the <code>results_processing.postprocessing</code> block, then rerun. The change
            overwrites this campaign's config in place (it is only postprocessing config, not
            captured data — the raw rosbags are untouched), so it can be re-run any number of
            times. The rerun starts in the background; this dialog closes so you can follow its
            progress in the campaign view.
          </Typography>
          {src.isError ? (
            <Alert severity="error">
              <ErrorText>{(src.error as Error).message}</ErrorText>
            </Alert>
          ) : null}
          <Paper variant="outlined" sx={{ height: 360, overflow: 'hidden' }}>
            <Editor
              height="360px"
              language="yaml"
              path={`${campaignId}.postprocessing.vast`}
              value={text ?? ''}
              onChange={(v) => setText(v ?? '')}
              theme="vs-dark"
              options={{
                minimap: { enabled: false },
                fontSize: 13,
                scrollBeyondLastLine: false,
                readOnly: src.isPending || busy,
              }}
            />
          </Paper>
          {saveAndRerun.isError ? (
            <Alert severity="error">
              <ErrorText>{(saveAndRerun.error as Error).message}</ErrorText>
            </Alert>
          ) : result && !result.ok ? (
            // A successful dispatch closes the dialog; only the busy guard lands here.
            <Alert severity="warning">
              <ErrorText>{result.message ?? 'Could not start the rerun.'}</ErrorText>
            </Alert>
          ) : null}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} color="inherit" disabled={busy}>
          Cancel
        </Button>
        <Button
          variant="contained"
          onClick={() => saveAndRerun.mutate()}
          disabled={busy || src.isPending || src.isError}
          startIcon={busy ? <CircularProgress size={16} color="inherit" /> : undefined}
        >
          {busy ? 'Starting…' : changed ? 'Save & rerun' : 'Rerun'}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
