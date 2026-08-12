import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'

// In production the robovast-service serves frontend/ui/dist itself, so the SPA is same-origin with the REST
// API and the client uses relative paths. In dev (this Vite server) we proxy the API path prefixes to
// a running `vast serve` so the browser stays same-origin here too (no CORS). Point at another service
// with ROBOVAST_SERVICE_URL. The RobovastInterface routes all live at the root, so we proxy them by
// prefix rather than a shared /api mount.
const SERVICE = process.env.ROBOVAST_SERVICE_URL ?? 'http://127.0.0.1:8800'

// Every top-level path segment the service owns. This list was hand-maintained and had
// drifted: `/results`, `/sources`, `/usage` and `/panel_types` were missing, so under
// `npm run dev` the file browser, the editor's load and save, uploads, run-view artifacts,
// the capacity meter and remote panel assets all failed against a service that was
// answering them perfectly well in production. Whenever this changes, check it against
// `Routes` in src/robovast/service/interface.py — the same table
// tests/service/test_route_docs.py holds the app to.
//
// Safe despite the SPA having its own /config page: navigation is **hash**-based
// (`#/config`), so the browser only ever requests `/`, and a `/config` prefix here cannot
// shadow it.
const API_PREFIXES = [
  // control
  '/campaigns', '/workspaces', '/uploads', '/image-builds', '/exec',
  // metadata + authoring help
  '/version', '/healthz', '/usage', '/config', '/variation_types', '/panel_types',
  // the two content namespaces (files by address)
  '/results', '/sources',
  // FastAPI's own pages
  '/docs', '/openapi.json',
]

// The heavy dependencies, split off the entry chunk and named so they cache independently.
// Grouped by library rather than by page: Monaco is shared by the config editor, the SQL
// editor and the postprocessing dialog, so a per-page split would ship it three times.
// Deliberately few, large chunks — each one is a request, and the service is often reached
// through a kubectl port-forward where round trips are the expensive part.
const VENDOR_CHUNKS: Record<string, string[]> = {
  monaco: ['monaco-editor', 'monaco-yaml', '@monaco-editor/react'],
  plotly: ['plotly.js-dist-min', 'react-plotly.js'],
  three: ['three'],
  vega: ['vega', 'vega-lite', 'react-vega'],
  mui: ['@mui/material', '@mui/icons-material', '@mui/x-data-grid', '@mui/x-tree-view',
        '@emotion/react', '@emotion/styled'],
  react: ['react', 'react-dom', '@tanstack/react-query'],
}

function vendorChunk(id: string): string | undefined {
  // Vite's own virtual helpers — the preload helper every dynamic import calls, and the
  // modulepreload polyfill. They must be pinned somewhere always-loaded. Left unassigned,
  // Rollup is free to park them in *any* chunk, and it chose `monaco`: the entry then
  // statically imported the 3.9 MB editor to get a 1 kB helper, which put Monaco back on
  // the critical path of a campaign list that never opens an editor. Measured, not
  // assumed — the entry chunk's `imports` named it.
  if (id.includes('vite/preload-helper') || id.includes('vite/modulepreload-polyfill')) {
    return 'react'
  }
  if (!id.includes('node_modules')) return undefined
  for (const [chunk, packages] of Object.entries(VENDOR_CHUNKS)) {
    // Match the package directory, not a bare substring: 'three' would otherwise also
    // capture anything with 'three' in its path.
    if (packages.some((p) => id.includes(`node_modules/${p}/`))) return chunk
  }
  return undefined
}

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      // The shared panel contract + clock scaffolding (also used by package-provided panel remotes,
      // which cannot import from here). In-tree source resolved by path rather than an installed
      // dependency; must match the tsconfig `paths` entry.
      '@robovast/panel-kit': fileURLToPath(new URL('../panel-kit/src/index.ts', import.meta.url)),
    },
  },
  build: {
    // The editor and charting vendors are legitimately large; warning about them on every
    // build trains people to ignore the warning. The real guard is the chunk table in CI.
    chunkSizeWarningLimit: 5000,
    rollupOptions: { output: { manualChunks: vendorChunk } },
  },
  server: {
    proxy: Object.fromEntries(
      API_PREFIXES.map((p) => [p, { target: SERVICE, changeOrigin: true }]),
    ),
  },
})
