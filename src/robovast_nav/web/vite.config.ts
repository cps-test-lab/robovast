// Builds robovast_nav's run-view panels as a single Module-Federation REMOTE. The output
// (remoteEntry.js + chunks) is written into the Python package as data (robovast_nav/web/dist)
// and served by the robovast service at /panel_types/<type>/assets/... . The host UI loads it at
// runtime (see ui/src/lib/remote.ts) and renders it with the PanelProps contract.
//
// One shared container ('robovast_nav') hosts every panel this package ships, so they share
// React/vendor chunks. Each panel type's Python class (robovast_nav/panels.py) sets REMOTE_NAME to
// this container name; the service then points each type's asset URL at this one bundle. Adding a
// panel = one more entry in `exposes` + one panel class.
//
// A remote is for a panel the host cannot otherwise serve. costmap needs its own service endpoint
// for binary grids. behaviorTree needs no renderer -- it *derives* from the host's built-in
// scenario tree (passed in via PanelProps.builtins) and only supplies nav2's table, title and
// empty-state hint, so a package panel does not mean a second copy of a panel.
//
// `react`/`react-dom` are shared singletons pinned to the host's version (^18) so the remote reuses
// the host's React rather than bundling its own -- a version mismatch here breaks loading.
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { federation } from '@module-federation/vite'

export default defineConfig({
  resolve: {
    // The panel contract and the clock-driven scaffolding, shared with the host UI as in-tree source
    // (../../../panel-kit) rather than a hand-copied `contract.ts`. Resolved by path, and deliberately
    // NOT an MF `shared` module: bundling our own copy means a version skew against a newer host is an
    // ordinary build rather than a remote-load failure. Must match the tsconfig `paths` entry.
    alias: {
      '@robovast/panel-kit': fileURLToPath(new URL('../../../panel-kit/src/index.ts', import.meta.url)),
    },
  },
  plugins: [
    react(),
    federation({
      // The container name MUST match the `name` the service emits in each panel's remote descriptor
      // (REMOTE_NAME in robovast_nav/panels.py); the runtime resolves shared deps by this name.
      name: 'robovast_nav',
      filename: 'remoteEntry.js',
      // No `@mf-types` emission. Nothing consumes it: the host types a remote through its own
      // `useRemoteComponent<PanelProps>` against @robovast/panel-kit, which both sides compile from
      // the same source -- so the contract is already checked at both ends, twice over, and a second
      // generated copy would only be another thing to drift. It also cannot work here, since the
      // declaration emit insists every source sit under this package's `src`, and the kit does not.
      dts: false,
      exposes: {
        // Each key is a panel's PANEL_MODULE in robovast_nav/panels.py:
        //   costmap      -> loadRemote('robovast_nav/costmap')
        //   behaviorTree -> loadRemote('robovast_nav/behaviorTree')
        './costmap': './src/costmap.tsx',
        './behaviorTree': './src/behaviorTree.tsx',
      },
      shared: {
        react: { singleton: true, requiredVersion: '^18' },
        'react-dom': { singleton: true, requiredVersion: '^18' },
      },
    }),
  ],
  build: {
    // MF remotes need a modern target (top-level await in the generated entry).
    target: 'chrome89',
    // Ship the build as package data inside the importable package.
    outDir: '../robovast_nav/web/dist',
    emptyOutDir: true,
  },
})
