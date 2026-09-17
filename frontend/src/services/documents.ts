import {
  DOCUMENT_STATUSES,
  FAILURE_REASONS,
  INGESTION_STAGES,
  type StoredDocument,
} from '../types/document'
import { API_MODE, request } from './api'
import { mockBackend } from './mockBackend'
import { asBoolean, asEnum, asItems, asNumber, asRecord, asString, optional } from './parse'

/**
 * The boundary of the system (anti-corruption layer): wire format in, domain
 * objects out. Everything the API dictates — snake_case, `null`, the `items`
 * envelope — stops here. Hooks and components never see it.
 */
export function toDocument(raw: unknown): StoredDocument {
  const value = asRecord(raw, 'document')
  return {
    id: asString(value.id, 'document.id'),
    filename: asString(value.filename, 'document.filename'),
    mimeType: asString(value.mime_type, 'document.mime_type'),
    sizeBytes: asNumber(value.size_bytes, 'document.size_bytes'),
    status: asEnum(value.status, 'document.status', DOCUMENT_STATUSES),
    createdAt: asString(value.created_at, 'document.created_at'),
    stage: optional(value.stage, 'document.stage', (input, field) =>
      asEnum(input, field, INGESTION_STAGES),
    ),
    pageCount: optional(value.page_count, 'document.page_count', asNumber),
    chunkCount: optional(value.chunk_count, 'document.chunk_count', asNumber),
    failureReason: optional(value.failure_reason, 'document.failure_reason', (input, field) =>
      asEnum(input, field, FAILURE_REASONS),
    ),
    retryable: asBoolean(value.retryable, 'document.retryable'),
  }
}

export async function listDocuments(): Promise<StoredDocument[]> {
  const payload = API_MODE === 'mock' ? await mockBackend.listDocuments() : await request<unknown>('/documents')
  return asItems(payload, 'documents').map(toDocument)
}

export async function uploadDocument(file: File): Promise<StoredDocument> {
  if (API_MODE === 'mock') return toDocument(await mockBackend.upload(file))
  const formData = new FormData()
  formData.append('file', file)
  // No Content-Type header: the browser sets the multipart boundary itself.
  const payload = await request<unknown>('/documents', {
    method: 'POST',
    body: formData,
    timeoutMs: 120_000,
  })
  return toDocument(payload)
}

export async function deleteDocument(id: string): Promise<void> {
  if (API_MODE === 'mock') return mockBackend.deleteDocument(id)
  await request<unknown>(`/documents/${id}`, { method: 'DELETE' })
}

export async function retryDocument(id: string): Promise<void> {
  if (API_MODE === 'mock') return mockBackend.retryDocument(id)
  await request<unknown>(`/documents/${id}/retry`, { method: 'POST' })
}
