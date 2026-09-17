/**
 * In-memory fake backend, used while the FastAPI service does not exist yet.
 * It implements EXACTLY the documented contract (same shapes, same states,
 * same failure modes), so switching to the real API is a one-line change in
 * the service modules — no component or hook is aware of it.
 */
import type { AnswerPayload } from '../types/chat'
import type { StoredDocument } from '../types/document'
import type { User } from '../types/auth'

const MOCK_USER: User = {
  id: 'f2a3c1d0-9b1e-4f6a-8f30-0c9b2f7d5a11',
  email: 'malak@knowpilot.dev',
  displayName: 'Malak Ben Hassine',
}

let signedIn = false
let documents: StoredDocument[] = []

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

export const mockBackend = {
  async getSession(): Promise<User | null> {
    await delay(400)
    return signedIn ? MOCK_USER : null
  },

  async signIn(): Promise<User> {
    await delay(600)
    signedIn = true
    return MOCK_USER
  },

  async signOut(): Promise<void> {
    await delay(250)
    signedIn = false
  },

  async listDocuments(): Promise<StoredDocument[]> {
    await delay(300)
    return documents.map((document) => ({ ...document }))
  },

  /** Accepts the file and starts a simulated indexing pipeline. */
  async upload(file: File): Promise<StoredDocument> {
    await delay(500)
    const id = crypto.randomUUID()
    const created: StoredDocument = {
      id,
      filename: file.name,
      mimeType: file.type || 'application/octet-stream',
      sizeBytes: file.size,
      status: 'processing',
      progress: 0,
      createdAt: new Date().toISOString(),
    }
    documents = [created, ...documents]
    void simulateIndexing(id, file)
    return { ...created }
  },

  async deleteDocument(id: string): Promise<void> {
    await delay(250)
    documents = documents.filter((document) => document.id !== id)
  },

  async retryDocument(id: string): Promise<void> {
    await delay(250)
    const document = documents.find((candidate) => candidate.id === id)
    if (!document) return
    document.status = 'processing'
    document.progress = 0
    delete document.failureReason
    void simulateIndexing(id)
  },

  async ask(question: string): Promise<AnswerPayload> {
    const readyDocuments = documents.filter((document) => document.status === 'ready')
    await delay(900)

    // A question about documents we do not have must NOT be answered.
    if (readyDocuments.length === 0 || question.trim().length < 12) {
      await delay(400)
      return { outcome: 'insufficient_evidence' }
    }

    // Deliberate failure hook, so the error state can be demonstrated.
    if (question.toLowerCase().includes('fail')) {
      throw new Error('mock backend failure')
    }

    await delay(1100)
    const primary = readyDocuments[0]
    const secondary = readyDocuments[1]
    return {
      outcome: 'answered',
      answer: [
        `Based on **${primary?.filename ?? 'your documents'}**, here is what I found:`,
        '',
        '- The retrieved passages cover the topic of your question.',
        '- Every statement below is grounded in the sources listed underneath.',
        '',
        '_This answer comes from the mock backend used during frontend development._',
      ].join('\n'),
      sources: [
        {
          id: crypto.randomUUID(),
          documentId: primary?.id ?? 'unknown',
          filename: primary?.filename ?? 'document.pdf',
          page: primary?.mimeType === 'application/pdf' ? 3 : undefined,
          snippet:
            'The termination period is thirty (30) days from the date of written notice, unless otherwise agreed by both parties.',
          score: 0.82,
        },
        ...(secondary
          ? [
              {
                id: crypto.randomUUID(),
                documentId: secondary.id,
                filename: secondary.filename,
                page: secondary.mimeType === 'application/pdf' ? 1 : undefined,
                snippet:
                  'Notices must be delivered in writing and are considered received on the next business day.',
                score: 0.64,
              },
            ]
          : []),
      ],
    }
  },
}

/** Moves a document through processing -> ready | failed, updating progress. */
async function simulateIndexing(id: string, file?: File): Promise<void> {
  for (let progress = 10; progress <= 100; progress += 15) {
    await delay(700)
    const document = documents.find((candidate) => candidate.id === id)
    if (!document) return
    document.progress = Math.min(progress, 100)
  }

  const document = documents.find((candidate) => candidate.id === id)
  if (!document) return

  // An empty file is the classic "PDF with no extractable text" case.
  if (file && file.size === 0) {
    document.status = 'failed'
    document.failureReason = 'no_text_found'
    delete document.progress
    return
  }

  document.status = 'ready'
  document.pageCount = document.mimeType === 'application/pdf' ? 12 : undefined
  document.chunkCount = 24
  delete document.progress
}
