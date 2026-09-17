import { useCallback, useEffect, useRef, useState } from 'react'
import * as documentsService from '../services/documents'
import type { StoredDocument } from '../types/document'

const POLL_INTERVAL_MS = 1500

interface DocumentsState {
  documents: StoredDocument[]
  isLoading: boolean
  loadError: boolean
}

/**
 * Server state for the documents list.
 * Polls only while at least one document is still being indexed: an idle list
 * costs zero requests.
 */
export function useDocuments() {
  const [state, setState] = useState<DocumentsState>({
    documents: [],
    isLoading: true,
    loadError: false,
  })
  const [uploadError, setUploadError] = useState<string | null>(null)
  const isMountedRef = useRef(true)

  const refresh = useCallback(async () => {
    try {
      const documents = await documentsService.listDocuments()
      if (!isMountedRef.current) return
      setState({ documents, isLoading: false, loadError: false })
    } catch {
      if (!isMountedRef.current) return
      setState((previous) => ({ ...previous, isLoading: false, loadError: true }))
    }
  }, [])

  useEffect(() => {
    isMountedRef.current = true
    // Fetching server state on mount IS synchronising with an external system;
    // the setState only runs after the await, never during this render pass.
    // oxlint-disable-next-line react/set-state-in-effect
    void refresh()
    return () => {
      isMountedRef.current = false
    }
  }, [refresh])

  const hasPendingWork = state.documents.some(
    (document) => document.status === 'processing' || document.status === 'uploading',
  )

  useEffect(() => {
    if (!hasPendingWork) return
    const timer = window.setInterval(() => void refresh(), POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [hasPendingWork, refresh])

  const upload = useCallback(
    async (file: File) => {
      setUploadError(null)
      // Optimistic row so the user sees the upload immediately.
      const optimistic: StoredDocument = {
        id: `pending-${crypto.randomUUID()}`,
        filename: file.name,
        mimeType: file.type || 'application/octet-stream',
        sizeBytes: file.size,
        status: 'uploading',
        retryable: false,
        createdAt: new Date().toISOString(),
      }
      setState((previous) => ({ ...previous, documents: [optimistic, ...previous.documents] }))

      try {
        await documentsService.uploadDocument(file)
        await refresh()
      } catch {
        if (!isMountedRef.current) return
        setState((previous) => ({
          ...previous,
          documents: previous.documents.filter((document) => document.id !== optimistic.id),
        }))
        setUploadError(`${file.name} could not be uploaded. Please try again.`)
      }
    },
    [refresh],
  )

  const remove = useCallback(
    async (id: string) => {
      await documentsService.deleteDocument(id)
      await refresh()
    },
    [refresh],
  )

  const retry = useCallback(
    async (id: string) => {
      await documentsService.retryDocument(id)
      await refresh()
    },
    [refresh],
  )

  return {
    documents: state.documents,
    isLoading: state.isLoading,
    loadError: state.loadError,
    uploadError,
    readyCount: state.documents.filter((document) => document.status === 'ready').length,
    upload,
    remove,
    retry,
    refresh,
    dismissUploadError: () => setUploadError(null),
  }
}
