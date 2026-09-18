# ADR-0010: OCR is a fallback, not a systematic step

- **Status:** Accepted
- **Date:** 2026-09-18

## Context

Uploaded PDFs come in three shapes. Most carry a real text layer and can be
read instantly. Some are scans: a photograph of a page, with no text at all.
A few are mixed, typically a born-digital report with two scanned annexes.

The ingestion pipeline must turn all of them into text, or say honestly that it
cannot. Optical character recognition can read a scan, but it costs seconds of
CPU per page, needs a system-level engine, and returns approximate text: a
misread character is indistinguishable from the real thing downstream.

The application also runs on free infrastructure and shares one machine with
PostgreSQL, Redis, Keycloak and the embedding model.

## Options considered

| Option | Pros | Cons |
| ------ | ---- | ---- |
| **Text layer first, OCR only on pages that came back nearly empty** | Normal documents cost nothing extra; scans are still recovered; the cost is proportional to the actual need | Two code paths to write and to test; a threshold to choose and defend |
| OCR every page systematically | One single code path; no threshold | Seconds of CPU per page on documents that never needed it; OCR output can be *worse* than the text layer we already had, silently degrading retrieval |
| No OCR at all | Simplest possible pipeline | Every scanned document is rejected, which is a large share of the contracts, invoices and old reports users upload |

## Decision

A two-tier pipeline, per page:

1. Extract the text layer with `pypdf`.
2. If a page yields fewer than `MIN_CHARACTERS_PER_PAGE` (40) characters, and an
   OCR engine was supplied, run OCR on that page only.
3. Keep the OCR result **only if it is longer** than what we already had.
4. If the whole document still holds less than `MIN_CHARACTERS_PER_DOCUMENT`
   (100) characters, fail with `no_text_found` and `retryable = false`.

OCR is capped at `MAX_OCR_PAGES` (30) pages per document, and the engine is
injected through an `OcrEngine` protocol rather than imported: passing `None`
disables the fallback entirely, which is how the API runs today.

## Consequences

- A normal document pays exactly zero OCR cost, and a test asserts it
  (`ocr.calls == 0`) so the property cannot regress unnoticed.
- A scanned document is recovered page by page; `ParsedPage.source` records
  which pages cost OCR, so the logs show what a slow upload was spent on.
- The page budget turns a 300-page scan from an outage into a partial result.
- The protocol keeps Tesseract out of the test environment and out of CI: the
  tests use a three-line fake, and a deliberately broken engine proves the
  pipeline degrades to `no_text_found` instead of returning a 500.
- Two thresholds are now policy we must defend. Set too low, the fallback never
  fires; set too high, readable pages are re-read badly. Both failures are
  silent, which is why they are named constants with tests around them.
- A page whose text is drawn as vector outlines, with no embedded image, is
  still unreadable. Rendering pages to bitmaps would fix it and is out of scope.

## Revisit when

Scanned uploads become common enough that the 30-page budget is hit regularly,
or users report documents failing with `no_text_found` that they can clearly
read. Either signal means it is time to add a real OCR engine behind the
existing protocol, run it outside the request path, and reconsider rendering
pages instead of only their embedded images.
