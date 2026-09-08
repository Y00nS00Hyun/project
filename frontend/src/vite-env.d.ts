/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  /** Development identity only; compiled out of production builds. */
  readonly VITE_DEBUG_USER_ID?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
