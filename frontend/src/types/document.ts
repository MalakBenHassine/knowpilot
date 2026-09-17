/**
 * Lifecycle of a document, as reported by the backend.
 * uploading  — bytes are being transferred (client side only)
 * processing — parsed, chunked, embedded and indexed server side
 * ready      — can be used to answer questions
 * failed     — could not be processed (e.g. scanned PDF with no text layer)
 */
export const DOCUMENT_STATUSES = ['uploading', 'processing', 'ready', 'failed'] as const
export type DocumentStatus = (typeof DOCUMENT_STATUSES)[number]

/**
 * Real step of the ingestion pipeline while `processing`.
 * The UI shows the actual stage instead of a made-up percentage.
 */
export const INGESTION_STAGES = ['parsing', 'chunking', 'embedding', 'indexing'] as const
export type IngestionStage = (typeof INGESTION_STAGES)[number]

/** Why a document could not be indexed. Backend codes, never raw errors. */
export const FAILURE_REASONS = [
  'no_text_found',
  'unsupported_format',
  'too_large',
  'processing_error',
] as const
export type DocumentFailureReason = (typeof FAILURE_REASONS)[number]

export interface StoredDocument {
  id: string
  filename: string
  mimeType: string
  sizeBytes: number
  status: DocumentStatus
  createdAt: string
  /** Only while status is "processing". */
  stage?: IngestionStage
  /** Known once the document is ready. */
  pageCount?: number
  chunkCount?: number
  /** Only set when status is "failed". */
  failureReason?: DocumentFailureReason
  /**
   * Whether retrying can plausibly succeed. Decided by the backend: a scanned
   * PDF never will, a timeout might. The UI only shows "Try again" when true.
   */
  retryable: boolean
}

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024
export const ACCEPTED_MIME_TYPES = ['application/pdf', 'text/plain'] as const
