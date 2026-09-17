import { createContext, useContext } from 'react'
import type { useDocuments } from './useDocuments'

export type DocumentsContextValue = ReturnType<typeof useDocuments>

export const DocumentsContext = createContext<DocumentsContextValue | null>(null)

/**
 * Shared because TWO screens need it: the documents page (list, upload) and the
 * chat (can a question be asked at all?). It is lifted no higher than that.
 */
export function useDocumentsContext(): DocumentsContextValue {
  const value = useContext(DocumentsContext)
  if (!value) throw new Error('useDocumentsContext must be used inside <DocumentsProvider>')
  return value
}
