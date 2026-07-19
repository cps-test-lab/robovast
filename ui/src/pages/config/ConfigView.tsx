import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import Editor from '@monaco-editor/react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import Divider from '@mui/material/Divider'
import List from '@mui/material/List'
import ListItemButton from '@mui/material/ListItemButton'
import MenuItem from '@mui/material/MenuItem'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import { robovast, type PreviewResponse, type ValidationReport } from '@/lib/robovastClient'
import { PreviewHost } from '@/preview/PreviewHost'
import { useEditableFile } from './useEditableFile'
import { useCreateVast } from './useCreateVast'

// The config editor only edits .vast project files; .osc scenario files are managed in the Files view.
const isVast = (p: string) => p.endsWith('.vast')

// The focus of the Config topic: pick (or create) a .vast, edit it in Monaco with live server-side
// validation, and Generate to preview the resolved configurations. No file tree — the .vast is
// auto-selected when there is one, otherwise chosen from a dropdown; other files live in the Files view.
export function ConfigView({ workspaceId }: { workspaceId: string }) {
  const [selected, setSelected] = useState('')
  const [validation, setValidation] = useState<ValidationReport | null>(null)
  const [preview, setPreview] = useState<PreviewResponse | null>(null)
  const [previewErr, setPreviewErr] = useState<string | null>(null)
  const [selectedCfg, setSelectedCfg] = useState(0)

  const files = useQuery({
    queryKey: ['files', workspaceId],
    queryFn: () => robovast.listProjectFiles(workspaceId),
    enabled: !!workspaceId,
  })

  const vastFiles = useMemo(
    () => (files.data?.files ?? []).map((f) => f.path).filter(isVast),
    [files.data],
  )
  // Auto-select the first .vast on startup / workspace change; when the selection vanishes
  // (e.g. workspace change) drop back to the first available so something is always picked.
  useEffect(() => {
    if (selected && !vastFiles.includes(selected)) setSelected(vastFiles[0] ?? '')
    else if (!selected && vastFiles.length) setSelected(vastFiles[0])
  }, [vastFiles, selected])

  const { content, saving, onChange } = useEditableFile(workspaceId, selected, async () => {
    setValidation(await robovast.validateProject(workspaceId, selected))
  })

  const generate = useMutation({
    mutationFn: () => robovast.previewConfigurations(workspaceId, 0, selected),
    onSuccess: (p) => {
      setPreview(p)
      setPreviewErr(null)
      setSelectedCfg(0)
    },
    onError: (e) => setPreviewErr((e as Error).message),
  })

  const allNames = useMemo(() => (files.data?.files ?? []).map((f) => f.path), [files.data])
  const createVast = useCreateVast(workspaceId, allNames, (name) => {
    setSelected(name)
    setValidation(null)
    setPreview(null)
  })

  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 2, minHeight: 460 }}>
      {/* Editor + validation */}
      <Stack spacing={1} sx={{ minWidth: 0 }}>
        <Stack direction="row" spacing={1} alignItems="center">
          {vastFiles.length > 1 ? (
            <TextField
              select
              size="small"
              label=".vast file"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              sx={{ minWidth: 200 }}
            >
              {vastFiles.map((p) => (
                <MenuItem key={p} value={p}>
                  {p}
                </MenuItem>
              ))}
            </TextField>
          ) : (
            <Typography variant="caption" sx={{ fontFamily: 'monospace' }}>
              {selected || '(no .vast yet)'}
            </Typography>
          )}
          <Box flexGrow={1} />
          <Button size="small" startIcon={<AddRoundedIcon />} onClick={createVast}>
            New .vast
          </Button>
          <Chip
            size="small"
            variant="outlined"
            label={saving === 'saving' ? 'saving…' : saving === 'saved' ? 'saved' : saving === 'error' ? 'save failed' : '—'}
            color={saving === 'error' ? 'error' : saving === 'saved' ? 'success' : 'default'}
          />
        </Stack>
        <Paper sx={{ height: 380, overflow: 'hidden' }}>
          <Editor
            height="380px"
            language="yaml"
            path={selected || 'config.vast'}
            value={content}
            onChange={onChange}
            theme="vs-dark"
            options={{ minimap: { enabled: false }, fontSize: 13, scrollBeyondLastLine: false, readOnly: !selected }}
          />
        </Paper>
        <ValidationPanel report={validation} />
      </Stack>

      {/* Preview */}
      <Stack spacing={1} sx={{ minWidth: 0 }}>
        <Stack direction="row" spacing={1} alignItems="center">
          <Button
            size="small"
            variant="contained"
            startIcon={<PlayArrowRoundedIcon />}
            onClick={() => generate.mutate()}
            disabled={!selected || generate.isPending}
          >
            Generate
          </Button>
          {preview ? (
            <Typography variant="caption" color="text.secondary">
              {preview.configs} configs · {preview.total_trials} trials
            </Typography>
          ) : null}
        </Stack>
        {previewErr ? <Alert severity="error">{previewErr}</Alert> : null}
        {preview ? (
          <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 1, minHeight: 0 }}>
            <Paper sx={{ p: 1, overflow: 'auto', maxHeight: 420 }}>
              <List dense disablePadding>
                {preview.configurations.map((c, i) => (
                  <ListItemButton
                    key={c.name}
                    selected={i === selectedCfg}
                    onClick={() => setSelectedCfg(i)}
                    sx={{ borderRadius: 1 }}
                  >
                    <Typography variant="caption" sx={{ fontFamily: 'monospace' }}>
                      {c.name}
                    </Typography>
                  </ListItemButton>
                ))}
              </List>
            </Paper>
            <Paper sx={{ p: 1, overflow: 'auto', maxHeight: 420 }}>
              {preview.configurations[selectedCfg] ? (
                <Stack spacing={1}>
                  <PreviewHost config={preview.configurations[selectedCfg]} />
                  <Typography variant="caption" color="text.secondary">
                    resolved parameters
                  </Typography>
                  <Box
                    component="pre"
                    sx={{ m: 0, fontFamily: 'monospace', fontSize: 12, whiteSpace: 'pre-wrap' }}
                  >
                    {JSON.stringify(preview.configurations[selectedCfg].parameters, null, 2)}
                  </Box>
                </Stack>
              ) : null}
            </Paper>
          </Box>
        ) : (
          <Alert severity="info" variant="outlined">
            Generate to preview the resolved configurations.
          </Alert>
        )}
      </Stack>
    </Box>
  )
}

function ValidationPanel({ report }: { report: ValidationReport | null }) {
  if (!report) {
    return (
      <Typography variant="caption" color="text.secondary">
        edit to validate…
      </Typography>
    )
  }
  if (report.valid) {
    return (
      <Alert severity="success" variant="outlined" sx={{ py: 0 }}>
        Valid · {report.configs} configs · {report.runs_per_config} runs/config ·{' '}
        {report.total_trials} trials
      </Alert>
    )
  }
  return (
    <Paper sx={{ p: 1, maxHeight: 120, overflow: 'auto', borderColor: 'error.main' }} variant="outlined">
      <Typography variant="caption" color="error">
        {report.problems.length} problem{report.problems.length === 1 ? '' : 's'}
      </Typography>
      <Divider sx={{ my: 0.5 }} />
      {report.problems.map((p, i) => (
        <Typography key={i} variant="caption" component="div" sx={{ fontFamily: 'monospace' }}>
          <b>{p.stage}</b>
          {p.config ? ` [${p.config}]` : ''}
          {p.field ? ` ${p.field}` : ''}: {p.message}
        </Typography>
      ))}
    </Paper>
  )
}
