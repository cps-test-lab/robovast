// Where the installable app's files land, relative to the app's base: written by the `pwa` Vite
// plugin (pwa.ts) and read by the page that registers the worker (appInstall.ts). One list, so the
// two cannot disagree. Free of `import.meta.env`, since the plugin reads it under Node.

export const PWA_FILES = {
  manifest: 'manifest.webmanifest',
  worker: 'sw.js',
} as const
