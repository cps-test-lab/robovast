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
// A remote is for a panel that needs something the host cannot provide -- costmap has its own
// service endpoint for binary grids. A package that merely produces a *table* in an existing schema
// needs no panel: it points a built-in one at its table with `source: { table }`, which is how
// nav2_behaviors is rendered by the core scenario_tree panel.
//
// `react`/`react-dom` are shared singletons pinned to the host's version (^18) so the remote reuses
// the host's React rather than bundling its own -- a version mismatch here breaks loading.
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { federation } from '@module-federation/vite'

export default defineConfig({
  plugins: [
    react(),
    federation({
      // The container name MUST match the `name` the service emits in each panel's remote descriptor
      // (REMOTE_NAME in robovast_nav/panels.py); the runtime resolves shared deps by this name.
      name: 'robovast_nav',
      filename: 'remoteEntry.js',
      exposes: {
        // Each key is a panel's PANEL_MODULE in robovast_nav/panels.py:
        //   costmap -> loadRemote('robovast_nav/costmap')
        './costmap': './src/costmap.tsx',
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
