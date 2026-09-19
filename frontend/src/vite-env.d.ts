/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the platform API. Empty means same origin, behind the proxy. */
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
