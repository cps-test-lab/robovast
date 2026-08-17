// ParametersPanel (type `parameters`): the scenario parameters of the selected configuration —
// what the trial is given.
//
// YAML rather than JSON because that is the language the .vast is written in: a value read here is
// usually on its way back into the file, and a reader should not have to translate braces into
// indentation to compare the two.
//
// The YAML is the whole panel. It previously carried two more blocks — a toggle for the
// `_`-prefixed internals a variation writes for other readers, and the per-variation previews (each
// factor's value list, with this configuration's value marked) — and both restated, in a second
// notation, what the file beside them already says. The column is narrow and shared with the
// geometry panels, so the space costs more than the restatement is worth.

import Box from '@mui/material/Box'
import type { ConfigPanelProps } from '@robovast/panel-kit'
import { registerConfigPanel } from '@/lib/panels/registry'
import { toYaml } from '@/lib/yaml'

function ParametersPanel({ config }: ConfigPanelProps) {
  return (
    <Box sx={{ height: '100%', overflow: 'auto', p: 1 }}>
      <Box component="pre" sx={{ m: 0, fontFamily: 'monospace', fontSize: 12, whiteSpace: 'pre-wrap' }}>
        {toYaml(config.parameters)}
      </Box>
    </Box>
  )
}

registerConfigPanel({
  manifest: { type: 'parameters', label: 'Scenario parameters' },
  component: ParametersPanel,
})

export default ParametersPanel
