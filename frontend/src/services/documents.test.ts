import { describe, expect, it } from 'vitest'
import { toDocument } from './documents'
import { InvalidResponseError } from './parse'

/** A valid payload, exactly as documented in docs/api/contract.md. */
function apiDocument(overrides: Record<string, unknown> = {}) {
  return {
    id: '0b7d3f3e-1c5a-4a9e-9f2e-3a7c1d2e5f80',
    filename: 'contract.pdf',
    mime_type: 'application/pdf',
    size_bytes: 2411724,
    status: 'ready',
    stage: null,
    page_count: 12,
    chunk_count: 24,
    failure_reason: null,
    retryable: false,
    created_at: '2026-09-17T18:12:04Z',
    ...overrides,
  }
}

describe('toDocument (wire format -> domain)', () => {
  it('maps snake_case to camelCase', () => {
    const document = toDocument(apiDocument())

    expect(document).toEqual({
      id: '0b7d3f3e-1c5a-4a9e-9f2e-3a7c1d2e5f80',
      filename: 'contract.pdf',
      mimeType: 'application/pdf',
      sizeBytes: 2411724,
      status: 'ready',
      createdAt: '2026-09-17T18:12:04Z',
      stage: undefined,
      pageCount: 12,
      chunkCount: 24,
      failureReason: undefined,
      retryable: false,
    })
  })

  it('turns null into undefined, the TypeScript convention', () => {
    const document = toDocument(apiDocument({ page_count: null, stage: null }))
    expect(document.pageCount).toBeUndefined()
    expect(document.stage).toBeUndefined()
  })

  it('keeps a processing stage', () => {
    const document = toDocument(apiDocument({ status: 'processing', stage: 'embedding' }))
    expect(document.status).toBe('processing')
    expect(document.stage).toBe('embedding')
  })

  it('ignores fields the frontend does not know about', () => {
    // A backend adding a field must never break an older client.
    const document = toDocument(apiDocument({ owner_id: 'someone-else', tenant: 'x' }))
    expect(document).not.toHaveProperty('owner_id')
  })

  describe('rejects invalid responses at runtime', () => {
    it('missing field', () => {
      const { size_bytes: _omitted, ...incomplete } = apiDocument()
      expect(() => toDocument(incomplete)).toThrow(InvalidResponseError)
    })

    it('wrong type', () => {
      expect(() => toDocument(apiDocument({ size_bytes: '2411724' }))).toThrow(InvalidResponseError)
    })

    it('unknown enumeration value', () => {
      expect(() => toDocument(apiDocument({ status: 'archived' }))).toThrow(InvalidResponseError)
    })

    it('null instead of a required boolean', () => {
      expect(() => toDocument(apiDocument({ retryable: null }))).toThrow(InvalidResponseError)
    })

    it('an HTML error page instead of JSON', () => {
      // What a proxy or a captive portal actually returns.
      expect(() => toDocument('<html>502 Bad Gateway</html>')).toThrow(InvalidResponseError)
    })
  })
})
