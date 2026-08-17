// ParametersPanel (type `parameters`): the scenario parameters of the selected configuration —
// what the trial is given.
//
// YAML rather than JSON because that is the language the .vast is written in: a value read here is
// usually on its way back into the file, and a reader should not have to translate braces into
// indentation to compare the two.
//
// The `_ internals` toggle adds the underscore-prefixed keys a variation wrote for other readers
// (`_map_file`, `_path`, `_goal_parameter_name`). They are a different LEVEL of the configuration,
// not a filtered subset of the parameters, so they are shown as their own block rather than merged
// in. This is what the desktop editor's `--debug` flag showed.

import { useState } from 'react'
import Box from '@mui/material/Box'
import FormControlLabel from '@mui/material/FormControlLabel'
import Stack from '@mui/material/Stack'
import Switch from '@mui/material/Switch'
import Typography from '@mui/material/Typography'
import type { ConfigPanelProps } from '@robovast/panel-kit'
import { registerConfigPanel } from '@/lib/panels/registry'
import { PreviewHost } from '@/preview/PreviewHost'
import { toYaml } from '@/lib/yaml'

function ParametersPanel({ config }: ConfigPanelProps) {
  const [showInternals, setShowInternals] = useState(false)
  const internalCount = Object.keys(config.internals ?? {}).length

  return (
    <Box sx={{ height: '100%', overflow: 'auto', p: 1 }}>
      <Stack spacing={1}>
        <Box component="pre" sx={{ m: 0, fontFamily: 'monospace', fontSize: 12, whiteSpace: 'pre-wrap' }}>
          {toYaml(config.parameters)}
        </Box>

        {internalCount ? (
          <>
            <FormControlLabel
              control={
                <Switch
                  size="small"
                  checked={showInternals}
                  onChange={(e) => setShowInternals(e.target.checked)}
                />
              }
              label={
                <Typography variant="caption" color="text.secondary">
                  {internalCount} internal value{internalCount === 1 ? '' : 's'}
                </Typography>
              }
            />
            {showInternals ? (
              <Box
                component="pre"
                sx={{
                  m: 0,
                  p: 1,
                  fontFamily: 'monospace',
                  fontSize: 12,
                  whiteSpace: 'pre-wrap',
                  bgcolor: 'action.hover',
                  borderRadius: 1,
                }}
              >
                {toYaml(config.internals)}
              </Box>
            ) : null}
          </>
        ) : null}

        {/* The per-variation previews (a distribution with this config's value marked). A
            different question from "what does this configuration contain" — what does the factor
            it came from look like — so it sits below rather than replacing anything. */}
        <PreviewHost config={config} />
      </Stack>
    </Box>
  )
}

registerConfigPanel({
  manifest: { type: 'parameters', label: 'Scenario parameters' },
  component: ParametersPanel,
})

export default ParametersPanel
