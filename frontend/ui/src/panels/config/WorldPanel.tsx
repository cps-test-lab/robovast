// WorldPanel (type `world`): the resolved `sim` block of the selected configuration — the world it
// runs in and the plugin overrides on it.
//
// A different level from the scenario parameters rather than more of them: one says what the trial
// does, the other what it runs in. A campaign that varies its environment (a floorplan per cell, a
// different obstacle population) varies it here, and nothing else in the UI showed that.
//
// `Describe world` is behind a button because it runs a container: it asks the simulator, in the
// campaign's own image, what the world actually offers — which plugin keys can be overridden and
// which entities it compiles. That is the answer that says whether a `sim:` key is a typo, and it
// costs an image pull to get, so it is never asked automatically.

import { useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { useMutation } from '@tanstack/react-query'
import type { ConfigPanelProps } from '@robovast/panel-kit'
import { registerConfigPanel } from '@/lib/panels/registry'
import { robovast } from '@/lib/robovastClient'
import { toYaml } from '@/lib/yaml'

function WorldPanel({ config, source }: ConfigPanelProps) {
  const [shown, setShown] = useState(false)
  const describe = useMutation({
    mutationFn: () => robovast.describeWorld(source.workspaceId, source.vastPath),
    onSuccess: () => setShown(true),
  })
  const world = describe.data

  return (
    <Box sx={{ height: '100%', overflow: 'auto', p: 1 }}>
      <Stack spacing={1}>
        <Box component="pre" sx={{ m: 0, fontFamily: 'monospace', fontSize: 12, whiteSpace: 'pre-wrap' }}>
          {toYaml(config.sim)}
        </Box>

        <Box>
          <Button
            size="small"
            onClick={() => describe.mutate()}
            disabled={describe.isPending}
            startIcon={describe.isPending ? <CircularProgress size={14} /> : undefined}
          >
            {describe.isPending ? 'Asking the simulator…' : 'Describe world'}
          </Button>
        </Box>

        {describe.isError ? (
          <Alert severity="warning" variant="outlined">
            {(describe.error as Error).message}
          </Alert>
        ) : null}

        {shown && world ? (
          <Stack spacing={0.5}>
            <Typography variant="caption" color="text.secondary">
              {world.backend} · {world.world || '(world from the campaign)'}
            </Typography>
            {/* The overridable keys are the vocabulary a `sim:` factor has to be spelled in, so
                they are the useful half of the answer; the entities matter only to a scenario
                that drives one by name. */}
            <Box component="pre" sx={{ m: 0, fontFamily: 'monospace', fontSize: 12, whiteSpace: 'pre-wrap' }}>
              {toYaml({ plugins: world.plugins, entities: world.entities })}
            </Box>
          </Stack>
        ) : null}
      </Stack>
    </Box>
  )
}

registerConfigPanel({
  manifest: { type: 'world', label: 'World configuration' },
  component: WorldPanel,
})

export default WorldPanel
