// Column 2 of the Config tab: Generate, and the list of configurations it produced.
//
// Split out of what used to be one preview pane so the list and the per-configuration view are
// separate columns: the list is a narrow index, the view beside it is where the space goes.

import Alert from '@mui/material/Alert'
import Button from '@mui/material/Button'
import List from '@mui/material/List'
import ListItemButton from '@mui/material/ListItemButton'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import { type ConfigEditor } from './useConfigEditor'

export function ConfigListPane({ editor }: { editor: ConfigEditor }) {
  const { selected, generate, preview, previewErr, selectedCfg, setSelectedCfg } = editor

  return (
    <Stack spacing={1} sx={{ minWidth: 0, height: '100%' }}>
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
      {previewErr ? <Alert severity="error">{previewErr}</Alert> : null}
      {preview ? (
        <Paper sx={{ p: 0.5, overflow: 'auto', flexGrow: 1, minHeight: 0 }}>
          <List dense disablePadding>
            {preview.configurations.map((c, i) => (
              <ListItemButton
                key={c.name}
                selected={i === selectedCfg}
                onClick={() => setSelectedCfg(i)}
                sx={{ borderRadius: 1, px: 1 }}
              >
                <Typography variant="caption" sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>
                  {c.name}
                </Typography>
              </ListItemButton>
            ))}
          </List>
        </Paper>
      ) : null}
    </Stack>
  )
}
