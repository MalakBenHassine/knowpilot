import type { StoredDocument } from '../types/document'
import { API_MODE, request } from './api'
import { mockBackend } from './mockBackend'

export async function listDocuments(): Promise<StoredDocument[]> {
  if (API_MODE === 'mock') return mockBackend.listDocuments()
  return request<StoredDocument[]>('/documents')
}

export async function uploadDocument(file: File): Promise<StoredDocument> {
  if (API_MODE === 'mock') return mockBackend.upload(file)
  const formData = new FormData()
  formData.append('file', file)
  // No Content-Type header: the browser sets the multipart boundary itself.
  return request<StoredDocument>('/documents', { method: 'POST', body: formData, timeoutMs: 120_000 })
}

export async function deleteDocument(id: string): Promise<void> {
  if (API_MODE === 'mock') return mockBackend.deleteDocument(id)
  await request<void>(`/documents/${id}`, { method: 'DELETE' })
}

export async function retryDocument(id: string): Promise<void> {
  if (API_MODE === 'mock') return mockBackend.retryDocument(id)
  await request<void>(`/documents/${id}/retry`, { method: 'POST' })
}
