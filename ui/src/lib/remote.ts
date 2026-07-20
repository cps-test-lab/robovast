// Shared Module-Federation loader for EXTERNAL plugins that ship a built remoteEntry.js
// (served by the service). Used by variation-type web previews (RemotePreview) and by the
// run-view's package/user-authored panels (PanelHost). The MF runtime is initialized once,
// seeding the host's React singletons so remotes share them (no duplicate React). Fully
// guarded: any failure surfaces as an error string so a broken/absent plugin never crashes
// the host.

import { useEffect, useState } from 'react'
import * as React from 'react'
import * as ReactDOM from 'react-dom'
import type { ComponentType } from 'react'

/** A Module-Federation remote descriptor, as emitted by the service (variation previews and
 *  run-view panels use the same shape). `module` is the exposed module id, e.g. `./costmap`. */
export interface RemoteDescriptor {
  name: string
  remote_entry_url: string
  module: string
}

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

/** Load a remote's exposed component at runtime. Returns `{Comp, err}`: `Comp` is null while
 *  loading, then the component (or stays null with `err` set on failure). The caller renders
 *  the loading/error states and passes the component its props. */
export function useRemoteComponent<P>(
  remote: RemoteDescriptor,
): { Comp: ComponentType<P> | null; err: string | null } {
  const [Comp, setComp] = useState<ComponentType<P> | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setComp(null)
    setErr(null)
    ;(async () => {
      try {
        const m = await mf()
        m.registerRemotes([{ name: remote.name, entry: remote.remote_entry_url }])
        const mod = (await m.loadRemote(`${remote.name}/${remote.module.replace(/^\.\//, '')}`)) as
          | { default?: ComponentType<P> }
          | ComponentType<P>
        if (!cancelled) {
          const C =
            (mod as { default?: ComponentType<P> }).default ?? (mod as ComponentType<P>)
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

  return { Comp, err }
}
