import { useCallback, useEffect, useRef, useState } from 'react'
import { formatWait } from '../lib/format'
import { ApiError } from '../services/api'
import * as documentsService from '../services/documents'
import type { StoredDocument } from '../types/document'

const POLL_INTERVAL_MS = 1500

/**
 * Turns a status code into something a person can act on.
 *
 * The backend deliberately returns codes and never sentences: the wording is a
 * product decision, it has to be translatable, and a technical message would
 * leak internals. Telling someone to "try again" when their file is simply too
 * large only wastes their time.
 */
export function uploadErrorMessage(file: File, error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.status) {
      case 409:
        return `${file.name} has already been uploaded.`
      case 413:
        return `${file.name} is too large. The limit is 20 MB.`
      case 415:
        return `${file.name} is not a PDF or a text file.`
      case 400:
        return `${file.name} is empty.`
      case 503:
        return 'Indexing is temporarily unavailable. Please try again in a moment.'
      case 429:
        // Without this case the generic "please try again" invited exactly
        // the immediate retry the limit exists to stop.
        return error.retryAfterSeconds === undefined
          ? `Too many uploads in a short time. Wait a moment before sending ${file.name}.`
          : `Too many uploads in a short time. Try ${file.name} again in ${formatWait(error.retryAfterSeconds)}.`
    }
  }
  return `${file.name} could not be uploaded. Please try again.`
}

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
      } catch (error) {
        if (!isMountedRef.current) return
        // The optimistic row is removed: leaving it would show a document that
        // does not exist on the server, and the next poll would erase it
        // anyway, which looks like a glitch rather than a refusal.
        setState((previous) => ({
          ...previous,
          documents: previous.documents.filter((document) => document.id !== optimistic.id),
        }))
        setUploadError(uploadErrorMessage(file, error))
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
