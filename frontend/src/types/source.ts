/**
 * A retrieved passage used to ground an answer.
 * Every field comes from the backend retrieval metadata — the UI never
 * invents a page number, a snippet or a score.
 */
export interface Source {
  id: string
  /**
   * The number the answer cites it by: [2] in the text is the card showing 2.
   * Without it, an answer reading "...[2]" above a single card leaves the
   * reader matching them by guesswork - found by testing the product by hand.
   */
  number?: number
  documentId: string
  filename: string
  /** Undefined for formats without pages (e.g. plain text). */
  page?: number
  snippet: string
  /** Similarity score, only rendered when the API provides it. */
  score?: number
}
