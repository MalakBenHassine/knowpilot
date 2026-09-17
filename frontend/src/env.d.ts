/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * "mock"  — the UI runs against an in-memory fake backend (default while the
   *           FastAPI service does not exist yet).
   * "http"  — real API calls to /api on the same origin.
   */
  readonly VITE_API_MODE?: 'mock' | 'http'
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
