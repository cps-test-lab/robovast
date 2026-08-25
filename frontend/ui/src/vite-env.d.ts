/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_ROBOVAST_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

// plotly.js-dist-min ships no types; @types/plotly.js covers the 'plotly.js' type-only imports.
declare module 'plotly.js-dist-min'
