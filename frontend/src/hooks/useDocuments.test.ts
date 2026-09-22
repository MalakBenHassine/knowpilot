import { describe, expect, it } from 'vitest'
import { ApiError } from '../services/api'
import { uploadErrorMessage } from './useDocuments'

const FILE = new File(['x'], 'contrat.pdf')

describe('uploadErrorMessage', () => {
  it('says when to send the file again after too many uploads', () => {
    const error = new ApiError('Request failed (429)', 429, 'rate_limited', 42)

    expect(uploadErrorMessage(FILE, error)).toBe(
      'Too many uploads in a short time. Try contrat.pdf again in a moment.',
    )
  })

  it('never answers a limit with an invitation to retry at once', () => {
    const error = new ApiError('Request failed (429)', 429, 'rate_limited')

    expect(uploadErrorMessage(FILE, error)).not.toMatch(/please try again/i)
  })
})
