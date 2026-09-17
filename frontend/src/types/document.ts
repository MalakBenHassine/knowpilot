/**
 * Lifecycle of a document, as reported by the backend.
 * uploading  — bytes are being transferred (client side only)
 * processing — parsed, chunked, embedded and indexed server side
 * ready      — can be used to answer questions
 * failed     — could not be processed (e.g. scanned PDF with no text layer)
 */
export type DocumentStatus = 'uploading' | 'processing' | 'ready' | 'failed'

/** Why a document could not be indexed. Backend codes, never raw errors. */
export type DocumentFailureReason =
  | 'no_text_found'
  | 'unsupported_format'
  | 'too_large'
  | 'processing_error'

export interface StoredDocument {
  id: string
  filename: string
  mimeType: string
  sizeBytes: number
  status: DocumentStatus
  createdAt: string
  /** Only meaningful while status is "uploading" or "processing" (0-100). */
  progress?: number
  /** Known once the document is ready. */
  pageCount?: number
  chunkCount?: number
  /** Only set when status is "failed". */
  failureReason?: DocumentFailureReason
}

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024
export const ACCEPTED_MIME_TYPES = ['application/pdf', 'text/plain'] as const
