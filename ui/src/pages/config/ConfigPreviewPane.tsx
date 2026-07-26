import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import List from '@mui/material/List'
import ListItemButton from '@mui/material/ListItemButton'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import { PreviewHost } from '@/preview/PreviewHost'
import { type ConfigEditor } from './useConfigEditor'

// Expand the selected .vast and list the resolved configurations with their parameters, without
// running anything. Always visible on the right, so switching the left column between Editor and
// Files never hides the preview of what the campaign will contain.
export function ConfigPreviewPane({ editor }: { editor: ConfigEditor }) {
  const { selected, generate, preview, previewErr, selectedCfg, setSelectedCfg } = editor

  return (
    <Stack spacing={1} sx={{ minWidth: 0, height: '100%' }}>
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
        <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 1, minHeight: 0, flexGrow: 1 }}>
          <Paper sx={{ p: 1, overflow: 'auto' }}>
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
          <Paper sx={{ p: 1, overflow: 'auto' }}>
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
  )
}
