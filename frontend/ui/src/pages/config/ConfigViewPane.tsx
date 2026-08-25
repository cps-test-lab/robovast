// Column 3 of the Config tab: the panels the .vast declares under `visualization.config.panels`,
// showing what the configuration selected in column 2 contains.
//
// The layout is the campaign author's — which panels, in what order, at what heights — the same way
// the run view's is. A project that declares nothing gets the two panels that need nothing from it
// (the service defaults them), so this column is never empty for want of a block nobody wrote.

import { useMemo } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import { ColumnHost } from '@/lib/panels/ColumnHost'
import { parseConfigPanels } from '@/lib/panels/parsePanels'
import { robovast } from '@/lib/robovastClient'
import { type ConfigEditor } from './useConfigEditor'
import '@/panels/config'

export function ConfigViewPane({
  editor,
  workspaceId,
}: {
  editor: ConfigEditor
  workspaceId: string
}) {
  const { preview, selected, selectedCfg } = editor
  const config = preview?.configurations[selectedCfg]

  const panels = useMemo(
    () => parseConfigPanels((preview?.config_panels ?? []) as Record<string, unknown>[]),
    [preview?.config_panels],
  )

  if (!preview) {
    return (
      <Alert severity="info" variant="outlined">
        Generate to see what each configuration contains.
      </Alert>
    )
  }
  if (!config) return null

  return (
    <Box sx={{ height: '100%', minHeight: 0 }}>
      <ColumnHost
        panels={panels}
        config={config}
        source={{ workspaceId, vastPath: selected }}
        fileUrl={(rel) => robovast.workspaceFileUrl(workspaceId, rel)}
      />
    </Box>
  )
}
