import { useEffect, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import IconButton from '@mui/material/IconButton'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import UploadFileRoundedIcon from '@mui/icons-material/UploadFileRounded'

import { CollapsibleBox } from '@/components/CollapsibleBox'
import { ErrorText } from '@/components/StatusView'
import { RobovastError, robovast, resultsUrl } from '@/lib/robovastClient'
import type { CampaignSummary } from '@/lib/robovastClient'
import { describeImport, describeImportError } from '@/lib/ingestReport'
import type { IngestReport, StageRow } from '@/lib/ingestReport'

// Bringing a campaign somebody else produced into this deployment: pick a .tar.gz, upload it,
// and it becomes a campaign here. The counterpart of a campaign card's archive download.
//
// Two calls, mirroring the service: the bytes are staged, then imported. Only the first is a
// transfer — the import itself is a TRACKED background operation, so the POST returns as soon
// as the work is under way and the campaign appears in the list at phase `importing`. There is
// nothing to poll for progress; the campaign row is the progress.
//
// What still has to be said here is the part the campaign row cannot say:
//
//  * A refusal before the import ever started — not a campaign archive, a name that is not a
//    campaign id, or a campaign of that id already here. No campaign row exists for these, so
//    without this panel they would vanish silently.
//  * What the import made of an OLD archive once it finished. The per-stage report is written
//    to the campaign's `_execution/import.json`, and a migration or a store rebuilt from the
//    results tree is exactly what somebody importing a two-versions-old campaign wants
//    confirmed. A blocking failure needs no help from here: the campaign goes to phase
//    `failed` with its error, which the card already shows in its own collapsible box.
//
// Everything is COLLAPSED by default, per the same rule the rest of the campaign view follows:
// the headline is the whole story for anyone who does not click, so any caveat has to reach the
// headline. `lib/ingestReport.ts` owns that wording and is tested on it.

/** One stage's row in the unfolded detail. Monospace name and a fixed indent, because these
 *  are versions and schema numbers people compare down the column, not prose. */
function StageLine({ row }: { row: StageRow }) {
  const colour =
    row.tone === 'error' ? 'error.main' : row.tone === 'warning' ? 'warning.main' : 'success.main'
  return (
    <Box sx={{ py: 0.5 }}>
      <Stack direction="row" spacing={1} alignItems="baseline">
        <Typography variant="caption" sx={{ minWidth: 110, fontFamily: 'monospace' }}>
          {row.name}
        </Typography>
        <Typography variant="caption" sx={{ color: colour, fontWeight: 600, minWidth: 70 }}>
          {row.label}
        </Typography>
      </Stack>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', pl: '118px' }}>
        {row.detail}
        {row.recovery ? ` — ${row.recovery}` : ''}
      </Typography>
    </Box>
  )
}

/** The import control and its outcome panel, as two separately placeable pieces.
 *
 *  A hook rather than one component because the two do not belong in the same box: the button
 *  is an item in the header's ROW, while the report is a block that has to sit under it. A
 *  component returning both would put a multi-line report inside the flex row beside the
 *  heading. The state is shared, so it lives in one place and hands back two elements.
 *
 *  `campaigns` is the live list the campaign view already has. It is passed in rather than
 *  fetched again because it is how this hook learns that the import it started has finished —
 *  the same stream the user is watching, so the panel and the row can never disagree. */
export function useCampaignImport(
  campaigns: CampaignSummary[] | undefined,
  onStarted?: () => void,
) {
  const [started, setStarted] = useState<{ id: string; note: string } | null>(null)
  const [report, setReport] = useState<IngestReport | null>(null)
  const [failure, setFailure] = useState<{ status?: number; message: string } | null>(null)
  const [open, setOpen] = useState(false)
  // The staged path is kept so a retry (Replace / Rebuild store) re-imports what was already
  // uploaded instead of asking for the file again — the bytes are on the service already, and a
  // multi-gigabyte re-upload just to set one flag would be an absurd thing to ask for.
  const staged = useRef<string | null>(null)

  const doImport = useMutation({
    mutationFn: async ({
      file,
      ...opts
    }: {
      file: File | null
      force?: boolean
      rebuildStore?: boolean
    }) => {
      setStarted(null)
      setReport(null)
      setFailure(null)
      setOpen(false)
      if (file) staged.current = (await robovast.stageCampaignArchive(file)).path
      if (!staged.current) throw new Error('nothing has been uploaded to import')
      return robovast.importCampaign(staged.current, opts)
    },
    onSuccess: (ref) => {
      // `note` is set when the archive carried no metric tables, so postprocessing follows the
      // extraction. Worth saying up front: otherwise the campaign appears to stall in a second
      // phase nobody asked for.
      setStarted({ id: ref.campaign_id, note: ref.note ?? '' })
      onStarted?.()
    },
    onError: (e: unknown) => {
      setFailure({
        status: e instanceof RobovastError ? e.status : undefined,
        message: e instanceof Error ? e.message : String(e),
      })
      // A refusal is the one thing worth unfolding unasked: the headline says what was wrong
      // in a few words, and the service's own message says what to do about it.
      setOpen(true)
    },
  })

  // The import we started has left its live phases, so its stage report has been written.
  // Fetched once, on that transition, rather than polled: `_execution/import.json` is only
  // interesting after the fact, and a campaign that failed has had its directory removed, so
  // there is deliberately no retry — the card's own failure box is the answer in that case.
  const settled = useSettledImport(campaigns, started?.id)
  useEffect(() => {
    if (!settled) return
    let live = true
    robovast
      .readFileAt(resultsUrl(settled, '_execution/import.json'))
      .then((file) => {
        if (live) setReport(JSON.parse(file.content) as IngestReport)
      })
      .catch(() => {
        /* No report to show: a failed import took its directory with it, and the campaign
           card reports that. Silence here rather than a second error about the first. */
      })
    return () => {
      live = false
    }
  }, [settled])

  const busy = doImport.isPending
  const outcome = report ? describeImport(report) : null
  const error = failure ? describeImportError(failure.status, failure.message) : null

  const button = (
    <Tooltip title={busy ? 'Uploading…' : 'Import a campaign archive (.tar.gz)'}>
      {/* `component="label"` makes the whole button the file input's label, which is the only
          way to open a picker without a visible <input> — the shape the config page's file
          upload already uses. */}
      <IconButton
        size="small"
        component="label"
        aria-label="Import a campaign archive"
        disabled={busy}
        sx={{ color: 'common.white' }}
      >
        {busy ? <CircularProgress size={18} /> : <UploadFileRoundedIcon fontSize="small" />}
        <input
          hidden
          type="file"
          accept=".tar.gz,.tgz,.tar,application/gzip,application/x-tar"
          onChange={(e) => {
            const file = e.target.files?.[0]
            // Cleared so picking the same file twice fires again — after a refusal the user's
            // next move may well be the same archive.
            e.target.value = ''
            if (file) doImport.mutate({ file })
          }}
        />
      </IconButton>
    </Tooltip>
  )

  const panel = (
    <>
      {/* Shown only until the report replaces it: while the import runs, the campaign row is
          the better place to look, and this just says which row that is. */}
      {started && !outcome ? (
        <Typography variant="caption" color="text.secondary">
          {`Importing ${started.id} — it appears in the list below.`}
          {started.note ? ` ${started.note}` : ''}
        </Typography>
      ) : null}

      {outcome ? (
        <ImportOutcomePanel
          headline={outcome.headline}
          tone={outcome.tone === 'error' ? 'error' : 'neutral'}
          open={open}
          onToggle={() => setOpen((o) => !o)}
          onDismiss={() => {
            setReport(null)
            setStarted(null)
          }}
          action={
            outcome.offerRebuild ? (
              <Button
                size="small"
                onClick={() => doImport.mutate({ file: null, force: true, rebuildStore: true })}
              >
                Rebuild store
              </Button>
            ) : null
          }
        >
          {outcome.rows.map((row) => (
            <StageLine key={row.name} row={row} />
          ))}
        </ImportOutcomePanel>
      ) : null}

      {error ? (
        <ImportOutcomePanel
          headline={error.headline}
          tone="error"
          open={open}
          onToggle={() => setOpen((o) => !o)}
          onDismiss={() => setFailure(null)}
          action={
            error.offerReplace ? (
              <Button
                size="small"
                color="error"
                onClick={() => doImport.mutate({ file: null, force: true })}
              >
                Replace existing
              </Button>
            ) : null
          }
        >
          <ErrorText>{error.detail}</ErrorText>
        </ImportOutcomePanel>
      ) : null}
    </>
  )

  return { button, panel: started || outcome || error ? panel : null }
}

/** The campaign id of *id*'s import once it has stopped working, else null.
 *
 *  "Stopped working" is read off the list rather than timed: an import rolls on into
 *  `postprocessing` when the archive was raw, so waiting for `importing` to end would fetch the
 *  report while postprocessing was still writing tables. */
function useSettledImport(campaigns: CampaignSummary[] | undefined, id: string | undefined) {
  const [settled, setSettled] = useState<string | null>(null)
  const wasLive = useRef(false)

  useEffect(() => {
    if (!id) {
      wasLive.current = false
      setSettled(null)
      return
    }
    const row = campaigns?.find((c) => c.campaign_id === id)
    if (!row) return
    const live = LIVE_IMPORT_PHASES.includes(row.phase ?? '')
    if (live) wasLive.current = true
    // Settled only after we actually saw it live. A campaign already finished when the POST
    // returned (a small archive on a fast disk) still qualifies via the second clause.
    if (!live && (wasLive.current || row.phase)) setSettled(id)
  }, [campaigns, id])

  return settled
}

const LIVE_IMPORT_PHASES = ['importing', 'postprocessing']

/** The outcome block. Rendered by the campaign view under its header row, so a long report
 *  pushes the campaign list down instead of stretching the header it came from. */
function ImportOutcomePanel({
  headline,
  tone,
  open,
  onToggle,
  onDismiss,
  action,
  children,
}: {
  headline: string
  tone: 'neutral' | 'error'
  open: boolean
  onToggle: () => void
  onDismiss: () => void
  action?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <CollapsibleBox
      title={headline}
      tone={tone}
      open={open}
      onToggle={onToggle}
      actions={
        <Stack direction="row" spacing={0.5} alignItems="center">
          {action}
          <Button size="small" color="inherit" onClick={onDismiss}>
            Dismiss
          </Button>
        </Stack>
      }
    >
      <Box sx={{ p: 1 }}>{children}</Box>
    </CollapsibleBox>
  )
}
