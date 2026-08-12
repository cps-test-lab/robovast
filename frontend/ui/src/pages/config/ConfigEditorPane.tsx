import Editor from '@monaco-editor/react'
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
  const { selected, setSelected, vastFiles, content, saving, onChange, validation, createVast } =
    editor

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
      <Paper sx={{ flexGrow: 1, minHeight: 0, overflow: 'hidden' }}>
        <Editor
          height="100%"
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
