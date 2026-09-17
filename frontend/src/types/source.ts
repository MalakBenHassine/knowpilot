/**
 * A retrieved passage used to ground an answer.
 * Every field comes from the backend retrieval metadata — the UI never
 * invents a page number, a snippet or a score.
 */
export interface Source {
  id: string
  documentId: string
  filename: string
  /** Undefined for formats without pages (e.g. plain text). */
  page?: number
  snippet: string
  /** Similarity score, only rendered when the API provides it. */
  score?: number
}
