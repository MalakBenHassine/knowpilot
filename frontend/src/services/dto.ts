/**
 * Wire format — exactly what the API sends (docs/api/contract.md).
 * snake_case, `null` instead of `undefined`, envelope around collections.
 * These types NEVER leave services/: the rest of the app uses domain types.
 */
export interface DocumentDto {
  id: string
  filename: string
  mime_type: string
  size_bytes: number
  status: string
  stage: string | null
  page_count: number | null
  chunk_count: number | null
  failure_reason: string | null
  retryable: boolean
  created_at: string
}

export interface ListDto<T> {
  items: T[]
}
