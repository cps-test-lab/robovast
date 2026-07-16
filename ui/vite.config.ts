import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'

// In production the robovast-service serves ui/dist itself, so the SPA is same-origin with the REST
// API and the client uses relative paths. In dev (this Vite server) we proxy the API path prefixes to
// a running `vast serve` so the browser stays same-origin here too (no CORS). Point at another service
// with ROBOVAST_SERVICE_URL. The RobovastInterface routes all live at the root, so we proxy them by
// prefix rather than a shared /api mount.
const SERVICE = process.env.ROBOVAST_SERVICE_URL ?? 'http://127.0.0.1:8800'
const API_PREFIXES = ['/campaigns', '/workspaces', '/uploads', '/version', '/healthz', '/config', '/variation_types', '/docs', '/openapi.json']

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    proxy: Object.fromEntries(
      API_PREFIXES.map((p) => [p, { target: SERVICE, changeOrigin: true }]),
    ),
  },
})
