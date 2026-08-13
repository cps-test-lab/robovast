import { useCallback, useEffect, useRef } from 'react'
import Editor from '@monaco-editor/react'
import type { editor as monacoEditor } from 'monaco-editor'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import Divider from '@mui/material/Divider'
import MenuItem from '@mui/material/MenuItem'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import { type ValidationReport } from '@/lib/robovastClient'
import { type ConfigEditor } from './useConfigEditor'

// The .vast editor: pick (or create) a .vast, edit it in Monaco with live server-side validation.
// No file tree — the .vast is auto-selected when there is one, otherwise chosen from a dropdown;
// other files live in the Files tab. Fills its container height so it reads as a full-height editor.
export function ConfigEditorPane({ editor }: { editor: ConfigEditor }) {
  const { selected, setSelected, vastFiles, content, saving, onChange, validation, createVast,
    readOnly } = editor

  const instance = useRef<monacoEditor.IStandaloneCodeEditor | null>(null)
  // Read inside the listener rather than captured: the editor is mounted once and kept alive
  // (see KeepAlive in App), so one instance serves both a workspace and, later, a campaign.
  const readOnlyNow = useRef(readOnly)
  readOnlyNow.current = readOnly

  // A read-only pane takes no cursor at all. `readOnly` alone still lets one be placed, which
  // invites the edit it then refuses; blocking the mousedown that would place it is the only
  // thing Monaco offers. Scrollbar drags are let through — this is about not typing, not about
  // making a long file unreadable — and the wheel never reaches here.
  const onMount = useCallback((ed: monacoEditor.IStandaloneCodeEditor) => {
    instance.current = ed
    const node = ed.getDomNode()
    node?.addEventListener('mousedown', (e) => {
      if (!readOnlyNow.current) return
      if ((e.target as HTMLElement).closest('.monaco-scrollable-element > .scrollbar')) return
      e.preventDefault()
      e.stopPropagation()
    }, true)
  }, [])

  // Keyboard is the other way in: drop the hidden textarea out of the tab order while read-only,
  // and let go of a cursor the pane already had when it was a workspace.
  useEffect(() => {
    const textarea = instance.current?.getDomNode()?.querySelector('textarea')
    if (!textarea) return
    textarea.tabIndex = readOnly ? -1 : 0
    if (readOnly) textarea.blur()
  }, [readOnly, selected])

  return (
    <Stack spacing={1} sx={{ minWidth: 0, height: '100%' }}>
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
        {/* Nothing here when read-only: there is no file to create and no save to report, and the
            banner above the page already says what this is. */}
        {readOnly ? null : (
          <>
            <Button size="small" startIcon={<AddRoundedIcon />} onClick={createVast}>
              New .vast
            </Button>
            <Chip
              size="small"
              variant="outlined"
              label={saving === 'saving' ? 'saving…' : saving === 'saved' ? 'saved' : saving === 'error' ? 'save failed' : '—'}
              color={saving === 'error' ? 'error' : saving === 'saved' ? 'success' : 'default'}
            />
          </>
        )}
      </Stack>
      <Paper
        sx={{
          flexGrow: 1,
          minHeight: 0,
          overflow: 'hidden',
          // Belt to the mousedown handler's braces: whatever else focuses the editor — a screen
          // reader, a stray programmatic focus — no caret is drawn where none can be placed.
          ...(readOnly ? { '& .monaco-editor .cursor': { display: 'none' } } : {}),
        }}
      >
        <Editor
          height="100%"
          language="yaml"
          path={selected || 'config.vast'}
          value={content}
          onChange={onChange}
          onMount={onMount}
          theme="vs-dark"
          options={{
            minimap: { enabled: false },
            fontSize: 13,
            scrollBeyondLastLine: false,
            readOnly: readOnly || !selected,
            domReadOnly: readOnly,
          }}
        />
      </Paper>
      <ValidationPanel report={validation} readOnly={readOnly} />
    </Stack>
  )
}

function ValidationPanel({ report, readOnly }: { report: ValidationReport | null; readOnly?: boolean }) {
  if (!report) {
    // Nothing at all when read-only: there is no edit to validate, and a line explaining the
    // absence of a feature is one more thing to read on every campaign config that is opened.
    if (readOnly) return null
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
