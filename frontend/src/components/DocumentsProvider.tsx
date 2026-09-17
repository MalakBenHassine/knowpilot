import type { ReactNode } from 'react'
import { DocumentsContext } from '../hooks/documents-context'
import { useDocuments } from '../hooks/useDocuments'

export function DocumentsProvider({ children }: { children: ReactNode }) {
  const documents = useDocuments()
  return <DocumentsContext.Provider value={documents}>{children}</DocumentsContext.Provider>
}
