// Renders an EXTERNAL variation plugin's web preview via the shared Module-Federation loader
// (see lib/remote.ts). The plugin ships a built remoteEntry.js (served at
// remote.remote_entry_url) exposing `./preview` — a React component `({config}) => JSX`. Fully
// guarded: any failure falls back to a note, so a broken/absent plugin never breaks the editor.
import Alert from '@mui/material/Alert'
import type { VariationRemote } from '@/lib/robovastClient'
import { useRemoteComponent } from '@/lib/remote'
import type { PreviewProps } from './builtins'

export function RemotePreview({
  remote,
  params,
  value,
}: { remote: VariationRemote } & PreviewProps) {
  const { Comp, err } = useRemoteComponent<PreviewProps>(remote)

  if (err) {
    return (
      <Alert severity="warning" variant="outlined" sx={{ py: 0 }}>
        plugin preview “{remote.name}” failed to load ({err})
      </Alert>
    )
  }
  if (!Comp) {
    return (
      <Alert severity="info" variant="outlined" sx={{ py: 0 }}>
        loading plugin preview “{remote.name}”…
      </Alert>
    )
  }
  return <Comp params={params} value={value} />
}
