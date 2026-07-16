// Renders a resolved configuration's per-variation previews. Built-in variation types render
// host-native (see builtins.tsx); external plugin types that ship a web asset render via the
// Module-Federation seam (RemotePreview). A type with neither shows nothing (the resolved
// parameters are already displayed alongside).
import Alert from '@mui/material/Alert'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { PreviewConfiguration, VariationPreview } from '@/lib/robovastClient'
import { BUILTIN_PREVIEWS } from './builtins'
import { RemotePreview } from './RemotePreview'

function paramValue(preview: VariationPreview, resolved: Record<string, unknown>): unknown {
  const name = preview.params.name
  return typeof name === 'string' ? resolved[name] : undefined
}

function OnePreview({
  preview,
  resolved,
}: {
  preview: VariationPreview
  resolved: Record<string, unknown>
}) {
  const Builtin = BUILTIN_PREVIEWS[preview.variation_type]
  const value = paramValue(preview, resolved)
  if (Builtin) return <Builtin params={preview.params} value={value} />
  if (preview.remote) return <RemotePreview remote={preview.remote} params={preview.params} value={value} />
  return null
}

export function PreviewHost({ config }: { config: PreviewConfiguration }) {
  const previews = config.previews ?? []
  if (!previews.length) {
    return (
      <Alert severity="info" variant="outlined" sx={{ py: 0 }}>
        No per-variation preview for this configuration.
      </Alert>
    )
  }
  return (
    <Stack spacing={1.5}>
      {previews.map((p, i) => (
        <Paper key={i} variant="outlined" sx={{ p: 1 }}>
          <Typography
            variant="caption"
            color="text.secondary"
            sx={{ fontFamily: 'monospace', display: 'block', mb: 0.5 }}
          >
            {p.variation_type}
          </Typography>
          <OnePreview preview={p} resolved={config.parameters} />
        </Paper>
      ))}
    </Stack>
  )
}
