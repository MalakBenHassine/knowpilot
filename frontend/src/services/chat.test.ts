import { describe, expect, it } from 'vitest'
import { formatWait } from '../lib/format'
import { toAnswerPayload, toSource } from './chat'
import { InvalidResponseError } from './parse'

/** A grounded answer, exactly as POST /api/chat returns it. */
function apiAnswer(overrides: Record<string, unknown> = {}) {
  return {
    answer: 'Les conges payes sont de 25 jours [1].',
    is_grounded: true,
    citations: [
      {
        number: 1,
        document_id: '0b7d3f3e-1c5a-4a9e-9f2e-3a7c1d2e5f80',
        filename: 'contrat.pdf',
        page_number: 7,
        snippet: 'Les conges payes sont de 25 jours par annee complete.',
      },
    ],
    ...overrides,
  }
}

describe('toAnswerPayload (wire format -> domain)', () => {
  it('maps a grounded answer to the answered outcome', () => {
    const payload = toAnswerPayload(apiAnswer())

    expect(payload).toEqual({
      outcome: 'answered',
      answer: 'Les conges payes sont de 25 jours [1].',
      sources: [
        {
          id: '0b7d3f3e-1c5a-4a9e-9f2e-3a7c1d2e5f80#1',
          number: 1,
          documentId: '0b7d3f3e-1c5a-4a9e-9f2e-3a7c1d2e5f80',
          filename: 'contrat.pdf',
          page: 7,
          snippet: 'Les conges payes sont de 25 jours par annee complete.',
        },
      ],
    })
  })

  it('turns an ungrounded answer into a normal outcome, not an error', () => {
    // The whole point of the feature: a refusal is the product working. It
    // must never reach the UI wearing an error style.
    const payload = toAnswerPayload(apiAnswer({ is_grounded: false, answer: '', citations: [] }))

    expect(payload).toEqual({ outcome: 'insufficient_evidence' })
  })

  it('does not expose a similarity score', () => {
    const [source] = toAnswerPayload(apiAnswer()).outcome === 'answered'
      ? (toAnswerPayload(apiAnswer()) as { sources: unknown[] }).sources
      : []

    // Showing 0.34 to somebody asking about their holidays is noise dressed as
    // precision. The snippet is the evidence; the distance is our business.
    expect(source).not.toHaveProperty('score')
  })

  it('rejects a grounded answer with no citations array', () => {
    // Grounded means cited, by construction on the server. Receiving one here
    // means the contract changed, which is a bug to surface, not to render.
    expect(() => toAnswerPayload(apiAnswer({ citations: null }))).toThrow(InvalidResponseError)
  })

  it('rejects a citation missing its snippet', () => {
    // A citation nobody can check is decoration. Better a visible failure than
    // a source card quoting undefined.
    expect(() => toSource({ ...apiAnswer().citations[0], snippet: null })).toThrow(
      InvalidResponseError,
    )
  })

  it('rejects a response that is not an object at all', () => {
    // A proxy error page or an older deployment would pass the type checker
    // and crash a component far from the cause.
    expect(() => toAnswerPayload('<html>502 Bad Gateway</html>')).toThrow(InvalidResponseError)
  })
})

describe('formatWait', () => {
  it('rounds up rather than down', () => {
    // Telling somebody to come back in 2 hours when the budget resets in 2 h 50
    // earns them one more refusal. Err towards patience.
    expect(formatWait(2 * 3600 + 50 * 60)).toBe('3 hours')
  })

  it('stays vague below a minute', () => {
    expect(formatWait(20)).toBe('a moment')
  })

  it('uses minutes below an hour', () => {
    expect(formatWait(600)).toBe('10 minutes')
  })
})
