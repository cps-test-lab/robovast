// Module-Federation loader for an EXTERNAL variation plugin's web preview. The plugin ships a built
// remoteEntry.js (served by the service at remote.remote_entry_url) exposing `./preview` — a React
// component `({config}) => JSX`. The MF runtime is initialized once, seeding the host's React
// singletons so the remote shares them (no duplicate React). Fully guarded: any failure falls back
// to a note, so a broken/absent plugin never breaks the editor.
//
// Runtime loading is verified against a real remote once an example plugin exists (a plugin ships
// its remote build as package data); the host seam + service asset route are in place.
import { useEffect, useState } from 'react'
import * as React from 'react'
import * as ReactDOM from 'react-dom'
import Alert from '@mui/material/Alert'
import type { ComponentType } from 'react'
import type { VariationRemote } from '@/lib/robovastClient'
import type { PreviewProps } from './builtins'

type MFRuntime = typeof import('@module-federation/enhanced/runtime')
let runtime: Promise<MFRuntime> | null = null

function mf(): Promise<MFRuntime> {
  if (!runtime) {
    runtime = import('@module-federation/enhanced/runtime').then((m) => {
      m.init({
        name: 'robovast_ui',
        remotes: [],
        shared: {
          react: { version: '18.3.1', lib: () => React, shareConfig: { singleton: true, requiredVersion: '^18' } },
          'react-dom': { version: '18.3.1', lib: () => ReactDOM, shareConfig: { singleton: true, requiredVersion: '^18' } },
        },
      })
      return m
    })
  }
  return runtime
}

export function RemotePreview({
  remote,
  params,
  value,
}: { remote: VariationRemote } & PreviewProps) {
  const [Comp, setComp] = useState<ComponentType<PreviewProps> | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const m = await mf()
        m.registerRemotes([{ name: remote.name, entry: remote.remote_entry_url }])
        const mod = (await m.loadRemote(
          `${remote.name}/${remote.module.replace(/^\.\//, '')}`,
        )) as { default?: ComponentType<PreviewProps> } | ComponentType<PreviewProps>
        if (!cancelled) {
          const C = (mod as { default?: ComponentType<PreviewProps> }).default ??
            (mod as ComponentType<PreviewProps>)
          setComp(() => C)
        }
      } catch (e) {
        if (!cancelled) setErr((e as Error).message)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [remote.name, remote.remote_entry_url, remote.module])

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
