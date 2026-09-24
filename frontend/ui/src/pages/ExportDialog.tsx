// Copyright (C) 2026 Frederik Pasch
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing,
// software distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions
// and limitations under the License.
//
// SPDX-License-Identifier: Apache-2.0

// The campaign menu's "Export…": which tables, in which format, with which bags, with or
// without the records -- then the export builds on the service and this shows its progress
// until the file is ready to download. The choices and the request they build are in
// `lib/campaignExport`; this is the rendering of them.

import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import CircularProgress from '@mui/material/CircularProgress'
import Dialog from '@mui/material/Dialog'
import DialogActions from '@mui/material/DialogActions'
import DialogContent from '@mui/material/DialogContent'
import DialogTitle from '@mui/material/DialogTitle'
import FormControl from '@mui/material/FormControl'
import FormControlLabel from '@mui/material/FormControlLabel'
import FormLabel from '@mui/material/FormLabel'
import Radio from '@mui/material/Radio'
import RadioGroup from '@mui/material/RadioGroup'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import DownloadRoundedIcon from '@mui/icons-material/DownloadRounded'

import { robovast, type ExportStatus } from '@/lib/robovastClient'
import { formatBytes } from '@/lib/format'
import {
  ALWAYS_EXPORTED,
  BAG_LABELS,
  FORMAT_LABELS,
  buildExportRequest,
  exportableTables,
  initialExportState,
  selectAllTables,
  toggleTable,
  type ExportBags,
  type ExportFormat,
  type ExportState,
} from '@/lib/campaignExport'

export function ExportDialog({
  campaignId,
  open,
  onClose,
}: {
  campaignId: string
  open: boolean
  onClose: () => void
}) {
  const describe = useQuery({
    queryKey: ['describe', campaignId],
    queryFn: () => robovast.describeCampaignData(campaignId),
    enabled: open,
  })

  const [state, setState] = useState<ExportState | null>(null)
  useEffect(() => {
    if (describe.data && state === null) {
      setState(initialExportState(exportableTables(describe.data.tables)))
    }
  }, [describe.data, state])

  const [exportId, setExportId] = useState<string | null>(null)
  const start = useMutation({
    mutationFn: (s: ExportState) => robovast.createExport(campaignId, buildExportRequest(s)),
    onSuccess: (ref) => setExportId(ref.export_id),
  })

  // Polled until it is over: the export builds on the service, and `done` with no `error`
  // is the moment the file exists.
  const status = useQuery({
    queryKey: ['export', campaignId, exportId],
    queryFn: () => robovast.getExportStatus(campaignId, exportId as string),
    enabled: !!exportId,
    refetchInterval: (q) => (q.state.data?.done ? false : 2000),
  })

  const ready = !!status.data?.done && !status.data.error
  const failed = status.data?.done && status.data.error

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>Export campaign</DialogTitle>
      <DialogContent dividers>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.5 }}>
          One <code>tar.gz</code> for an analysis away from the service: the tables as one
          file each, the campaign&apos;s records beside them, and its recordings if asked.
          The service builds it; the download link appears here when it is done.
        </Typography>

        {exportId === null ? (
          <Choices
            state={state}
            loading={describe.isLoading}
            failed={describe.isError ? String((describe.error as Error).message) : null}
            onChange={setState}
          />
        ) : (
          <Progress
            status={status.data}
            failed={status.isError ? String((status.error as Error).message) : null}
            href={ready ? robovast.exportUrl(campaignId, exportId) : null}
            fileName={`${campaignId}-export-${exportId}.tar.gz`}
          />
        )}

        {start.isError ? (
          <Alert severity="error" variant="outlined" sx={{ mt: 1.5 }}>
            {String((start.error as Error).message)}
          </Alert>
        ) : null}
        {failed ? (
          <Alert severity="error" variant="outlined" sx={{ mt: 1.5 }}>
            {status.data?.error}
          </Alert>
        ) : null}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{ready || failed ? 'Close' : 'Cancel'}</Button>
        {exportId === null ? (
          <Button
            variant="contained"
            disabled={state === null || start.isPending}
            onClick={() => state && start.mutate(state)}
          >
            Start
          </Button>
        ) : null}
      </DialogActions>
    </Dialog>
  )
}

function Choices({
  state,
  loading,
  failed,
  onChange,
}: {
  state: ExportState | null
  loading: boolean
  failed: string | null
  onChange: (next: ExportState) => void
}) {
  if (failed) return <Alert severity="error" variant="outlined">{failed}</Alert>
  if (loading || state === null) {
    return (
      <Stack direction="row" spacing={1} alignItems="center">
        <CircularProgress size={16} />
        <Typography variant="body2">Reading the campaign&apos;s tables…</Typography>
      </Stack>
    )
  }
  const allChecked = state.tables.every((t) => state.selected.has(t))
  const someChecked = state.tables.some((t) => state.selected.has(t))
  return (
    <Stack spacing={2}>
      <FormControl component="fieldset">
        <FormLabel component="legend">Tables</FormLabel>
        <FormControlLabel
          label={<Typography variant="body2">All tables</Typography>}
          control={
            <Checkbox
              size="small"
              checked={allChecked}
              indeterminate={!allChecked && someChecked}
              onChange={(e) => onChange(selectAllTables(state, e.target.checked))}
            />
          }
        />
        <Stack sx={{ pl: 3, maxHeight: 220, overflowY: 'auto' }}>
          <FormControlLabel
            label={<Typography variant="body2">{ALWAYS_EXPORTED} (always)</Typography>}
            control={<Checkbox size="small" checked disabled />}
          />
          {state.tables.map((table) => (
            <FormControlLabel
              key={table}
              label={<Typography variant="body2">{table}</Typography>}
              control={
                <Checkbox
                  size="small"
                  checked={state.selected.has(table)}
                  onChange={() => onChange(toggleTable(state, table))}
                />
              }
            />
          ))}
        </Stack>
      </FormControl>

      <FormControl component="fieldset">
        <FormLabel component="legend">Format</FormLabel>
        <RadioGroup
          value={state.format}
          onChange={(e) => onChange({ ...state, format: e.target.value as ExportFormat })}
        >
          {(Object.keys(FORMAT_LABELS) as ExportFormat[]).map((format) => (
            <FormControlLabel
              key={format}
              value={format}
              control={<Radio size="small" />}
              label={<Typography variant="body2">{FORMAT_LABELS[format]}</Typography>}
            />
          ))}
        </RadioGroup>
      </FormControl>

      <FormControl component="fieldset">
        <FormLabel component="legend">Recordings</FormLabel>
        <RadioGroup
          value={state.bags}
          onChange={(e) => onChange({ ...state, bags: e.target.value as ExportBags })}
        >
          {(Object.keys(BAG_LABELS) as ExportBags[]).map((bags) => (
            <FormControlLabel
              key={bags}
              value={bags}
              control={<Radio size="small" />}
              label={<Typography variant="body2">{BAG_LABELS[bags]}</Typography>}
            />
          ))}
        </RadioGroup>
      </FormControl>

      <FormControlLabel
        label={
          <Typography variant="body2">
            Include the records (campaign.db, the frozen config, every run&apos;s own files)
          </Typography>
        }
        control={
          <Checkbox
            size="small"
            checked={state.records}
            onChange={(e) => onChange({ ...state, records: e.target.checked })}
          />
        }
      />
    </Stack>
  )
}

function Progress({
  status,
  failed,
  href,
  fileName,
}: {
  status: ExportStatus | undefined
  failed: string | null
  href: string | null
  fileName: string
}) {
  if (failed) return <Alert severity="error" variant="outlined">{failed}</Alert>
  const written = Object.keys(status?.tables ?? {}).length
  if (href) {
    return (
      <Stack spacing={1} alignItems="flex-start">
        <Typography variant="body2">
          Done: {written} table{written === 1 ? '' : 's'}, {formatBytes(status?.bytes ?? 0)}.
        </Typography>
        <Button
          component="a"
          href={href}
          download={fileName}
          variant="contained"
          startIcon={<DownloadRoundedIcon />}
        >
          Download {fileName}
        </Button>
      </Stack>
    )
  }
  if (status?.done) return null
  return (
    <Stack direction="row" spacing={1} alignItems="center">
      <CircularProgress size={16} />
      <Typography variant="body2">
        Building… {written} table{written === 1 ? '' : 's'} written
      </Typography>
    </Stack>
  )
}
