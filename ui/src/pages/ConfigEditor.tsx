import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Editor from '@monaco-editor/react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import Divider from '@mui/material/Divider'
import IconButton from '@mui/material/IconButton'
import List from '@mui/material/List'
import ListItemButton from '@mui/material/ListItemButton'
import MenuItem from '@mui/material/MenuItem'
import Paper from '@mui/material/Paper'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import UploadFileRoundedIcon from '@mui/icons-material/UploadFileRounded'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { robovast, type PreviewResponse, type ValidationReport } from '@/lib/robovastClient'
import { configureVastSchema, isSchemaConfigured } from '@/lib/monaco'
import { PreviewHost } from '@/preview/PreviewHost'

const isVast = (p: string) => p.endsWith('.vast') || p.endsWith('.osc')

// Web equivalent of `vast config gui`: pick a workspace, edit its .vast in Monaco with live
// (debounced) server-side validation, and Generate to preview the resolved configurations. The
// workspace is the project — linked files (scenario/run files) are uploaded into it.
export function ConfigEditor() {
  const qc = useQueryClient()
  const [workspaceId, setWorkspaceId] = useState('')
  const [selected, setSelected] = useState('') // the .vast path being edited
  const [content, setContent] = useState('')
  const [validation, setValidation] = useState<ValidationReport | null>(null)
  const [saving, setSaving] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [preview, setPreview] = useState<PreviewResponse | null>(null)
  const [previewErr, setPreviewErr] = useState<string | null>(null)
  const [selectedCfg, setSelectedCfg] = useState(0)
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const workspaces = useQuery({ queryKey: ['workspaces'], queryFn: () => robovast.listWorkspaces() })
  const files = useQuery({
    queryKey: ['files', workspaceId],
    queryFn: () => robovast.listProjectFiles(workspaceId),
    enabled: !!workspaceId,
  })
  // Load the .vast JSON schema once and wire it into Monaco (completion + inline validation).
  useQuery({
    queryKey: ['configSchema'],
    queryFn: async () => {
      const schema = await robovast.getConfigSchema()
      if (!isSchemaConfigured()) configureVastSchema(schema)
      return schema
    },
    staleTime: Infinity,
  })

  const createWs = useMutation({
    mutationFn: () => robovast.createWorkspace(),
    onSuccess: (ws) => {
      qc.invalidateQueries({ queryKey: ['workspaces'] })
      setWorkspaceId(ws.workspace_id)
    },
  })

  // Auto-pick the single .vast when a workspace's files load.
  const vastFiles = useMemo(
    () => (files.data?.files ?? []).map((f) => f.path).filter(isVast),
    [files.data],
  )
  // Auto-select only when there's exactly one .vast; with several, the user picks in the file list.
  useEffect(() => {
    if (!selected && vastFiles.length === 1) setSelected(vastFiles[0])
  }, [vastFiles, selected])

  // Load the selected file's content into the buffer.
  useEffect(() => {
    if (!workspaceId || !selected) return
    let cancelled = false
    robovast.readProjectFile(workspaceId, selected).then((f) => {
      if (!cancelled) {
        setContent(f.content)
        setSaving('idle')
      }
    })
    return () => {
      cancelled = true
    }
  }, [workspaceId, selected])

  // Debounced autosave + validate (mirrors the desktop 500ms validation timer, server-side).
  const scheduleSave = useCallback(
    (text: string) => {
      if (!workspaceId || !selected) return
      if (saveTimer.current) clearTimeout(saveTimer.current)
      setSaving('saving')
      saveTimer.current = setTimeout(async () => {
        try {
          await robovast.writeProjectFile(workspaceId, selected, text)
          setSaving('saved')
          setValidation(await robovast.validateProject(workspaceId, selected))
        } catch (e) {
          setSaving('error')
          setValidation({
            valid: false,
            problems: [{ stage: 'error', message: (e as Error).message }],
            configs: 0,
            runs_per_config: 0,
            total_trials: 0,
          })
        }
      }, 600)
    },
    [workspaceId, selected],
  )

  const onEditorChange = (value?: string) => {
    const text = value ?? ''
    setContent(text)
    scheduleSave(text)
  }

  const generate = useMutation({
    mutationFn: () => robovast.previewConfigurations(workspaceId, 0, selected),
    onSuccess: (p) => {
      setPreview(p)
      setPreviewErr(null)
      setSelectedCfg(0)
    },
    onError: (e) => setPreviewErr((e as Error).message),
  })

  const upload = async (file: File) => {
    await robovast.uploadFile(workspaceId, file.name, file)
    qc.invalidateQueries({ queryKey: ['files', workspaceId] })
  }

  return (
    <Stack spacing={2}>
      {/* Workspace bar */}
      <Stack direction="row" spacing={2} alignItems="center">
        <Typography variant="h6">Config editor</Typography>
        <TextField
          select={!!workspaces.data?.workspaces.length}
          size="small"
          label="Workspace"
          value={workspaceId}
          onChange={(e) => {
            setWorkspaceId(e.target.value)
            setSelected('')
            setPreview(null)
            setValidation(null)
          }}
          sx={{ minWidth: 240 }}
        >
          {(workspaces.data?.workspaces ?? []).map((w) => (
            <MenuItem key={w.workspace_id} value={w.workspace_id}>
              {w.name || w.workspace_id}
              {w.read_only ? (
                <Chip
                  label="read-only"
                  size="small"
                  variant="outlined"
                  sx={{ ml: 1, height: 18, fontSize: '0.65rem' }}
                />
              ) : null}
            </MenuItem>
          ))}
        </TextField>
        <Button size="small" onClick={() => createWs.mutate()} disabled={createWs.isPending}>
          New workspace
        </Button>
      </Stack>

      {!workspaceId ? (
        <Alert severity="info" variant="outlined">
          Select or create a workspace, then upload your scenario/run files and author a{' '}
          <code>.vast</code>.
        </Alert>
      ) : (
        <Box sx={{ display: 'grid', gridTemplateColumns: '220px 1fr 1fr', gap: 2, minHeight: 460 }}>
          {/* File panel */}
          <Paper sx={{ p: 1.5, overflow: 'auto' }}>
            <Stack direction="row" alignItems="center" justifyContent="space-between" mb={1}>
              <Typography variant="subtitle2">Files</Typography>
              <IconButton size="small" component="label" title="Upload a linked file">
                <UploadFileRoundedIcon fontSize="small" />
                <input
                  hidden
                  type="file"
                  onChange={(e) => {
                    const f = e.target.files?.[0]
                    if (f) upload(f)
                    e.target.value = ''
                  }}
                />
              </IconButton>
            </Stack>
            <List dense disablePadding>
              {(files.data?.files ?? []).map((f) => (
                <ListItemButton
                  key={f.path}
                  selected={f.path === selected}
                  onClick={() => isVast(f.path) && setSelected(f.path)}
                  sx={{ borderRadius: 1 }}
                >
                  <Typography
                    variant="caption"
                    sx={{ fontFamily: 'monospace', color: isVast(f.path) ? 'text.primary' : 'text.secondary' }}
                  >
                    {f.path}
                  </Typography>
                </ListItemButton>
              ))}
              {!files.data?.files.length ? (
                <Typography variant="caption" color="text.secondary">
                  no files — upload a scenario file, then create a <code>.vast</code>
                </Typography>
              ) : null}
            </List>
          </Paper>

          {/* Editor + validation */}
          <Stack spacing={1} sx={{ minWidth: 0 }}>
            <Stack direction="row" spacing={1} alignItems="center">
              <Typography variant="caption" sx={{ fontFamily: 'monospace' }}>
                {selected || '(no .vast selected)'}
              </Typography>
              <Box flexGrow={1} />
              <Chip
                size="small"
                variant="outlined"
                label={saving === 'saving' ? 'saving…' : saving === 'saved' ? 'saved' : saving === 'error' ? 'save failed' : '—'}
                color={saving === 'error' ? 'error' : saving === 'saved' ? 'success' : 'default'}
              />
            </Stack>
            <Paper sx={{ height: 320, overflow: 'hidden' }}>
              <Editor
                height="320px"
                language="yaml"
                path={selected || 'config.vast'}
                value={content}
                onChange={onEditorChange}
                theme="vs-dark"
                options={{ minimap: { enabled: false }, fontSize: 13, scrollBeyondLastLine: false }}
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
                <Paper sx={{ p: 1, overflow: 'auto', maxHeight: 380 }}>
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
                <Paper sx={{ p: 1, overflow: 'auto', maxHeight: 380 }}>
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
      )}
    </Stack>
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
