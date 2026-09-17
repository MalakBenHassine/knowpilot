/**
 * In-memory fake backend, used while the FastAPI service is being built.
 *
 * It answers with the WIRE FORMAT of docs/api/contract.md (snake_case, `items`
 * envelope, `null` for absent values), so the real parsing and mapping code
 * runs in mock mode too. Switching to the real API exercises no new code path.
 */
import type { DocumentDto, ListDto } from './dto'
import type { AnswerPayload } from '../types/chat'
import type { User } from '../types/auth'

const MOCK_USER: User = {
  id: 'f2a3c1d0-9b1e-4f6a-8f30-0c9b2f7d5a11',
  email: 'malak@knowpilot.dev',
  displayName: 'Malak Ben Hassine',
}

const STAGES = ['parsing', 'chunking', 'embedding', 'indexing'] as const

let signedIn = false
let documents: DocumentDto[] = []

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

  async listDocuments(): Promise<ListDto<DocumentDto>> {
    await delay(300)
    return { items: documents.map((document) => ({ ...document })) }
  },

  async upload(file: File): Promise<DocumentDto> {
    await delay(500)
    const created: DocumentDto = {
      id: crypto.randomUUID(),
      filename: file.name,
      mime_type: file.type || 'application/octet-stream',
      size_bytes: file.size,
      status: 'processing',
      stage: 'parsing',
      page_count: null,
      chunk_count: null,
      failure_reason: null,
      retryable: false,
      created_at: new Date().toISOString(),
    }
    documents = [created, ...documents]
    void simulateIndexing(created.id, file)
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
    document.stage = 'parsing'
    document.failure_reason = null
    void simulateIndexing(id)
  },

  async ask(question: string): Promise<AnswerPayload> {
    const readyDocuments = documents.filter((document) => document.status === 'ready')
    await delay(900)

    // A question we have no evidence for must NOT be answered.
    if (readyDocuments.length === 0 || question.trim().length < 12) {
      await delay(400)
      return { outcome: 'insufficient_evidence' }
    }

    // Deliberate failure hook, so the error state can be exercised.
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
          page: primary?.mime_type === 'application/pdf' ? 3 : undefined,
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
                page: secondary.mime_type === 'application/pdf' ? 1 : undefined,
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

/** Walks a document through the real pipeline stages, then ready | failed. */
async function simulateIndexing(id: string, file?: File): Promise<void> {
  for (const stage of STAGES) {
    await delay(1200)
    const document = documents.find((candidate) => candidate.id === id)
    if (!document) return
    document.stage = stage
  }

  const document = documents.find((candidate) => candidate.id === id)
  if (!document) return

  // An empty file stands for the classic "PDF with no extractable text".
  if (file && file.size === 0) {
    document.status = 'failed'
    document.stage = null
    document.failure_reason = 'no_text_found'
    document.retryable = false // permanent: retrying would fail again
    return
  }

  document.status = 'ready'
  document.stage = null
  document.page_count = document.mime_type === 'application/pdf' ? 12 : null
  document.chunk_count = 24
}
