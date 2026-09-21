"""Uploaded files as LangChain documents: one `Document` per page.

A custom `BaseLoader` rather than `PyPDFLoader` from langchain-community. The
community loader reads a PDF; ours also enforces the limits an upload needs -
500 pages at most, a minimum amount of text, the OCR fallback of ADR-0010 -
and those rules are what stops a hostile file from costing ten minutes of CPU.
LangChain's own documentation recommends a custom loader for exactly this case:
the interface is the standard, the implementation is yours.

Every page carries the metadata a citation needs, following the convention of
the standard PDF loaders (`page`, `total_pages`, `source`), so downstream code
never has to know which loader produced a document.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document

from app.rag.parsing import OcrEngine, ParsedDocument, parse_document


class UploadedFileLoader(BaseLoader):
    """Load one stored upload, page by page."""

    def __init__(
        self, path: Path, mime_type: str, *, filename: str, ocr: OcrEngine | None = None
    ) -> None:
        self._path = path
        self._mime_type = mime_type
        # The name the user uploaded, not the path on disk: the storage path is
        # an internal detail and must never reach a prompt or a response.
        self._filename = filename
        self._ocr = ocr

    def lazy_load(self) -> Iterator[Document]:
        """Yield pages as they are parsed.

        Raises the `ParsingError` family unchanged: the pipeline turns those
        into a failure reason the user can read, and a loader that swallowed
        them would index an empty document as a success.
        """
        parsed = parse_document(self._path, self._mime_type, self._ocr)
        yield from pages_of(parsed, filename=self._filename)


def pages_of(parsed: ParsedDocument, *, filename: str) -> Iterator[Document]:
    """Convert our parser's output into LangChain documents.

    Separate from the loader so it can be tested without a file on disk.
    """
    for page in parsed.pages:
        yield Document(
            page_content=page.text,
            metadata={
                "source": filename,
                # 1-based, the way a human counts pages - and the way the page
                # is printed in a citation.
                "page_number": page.number,
                "total_pages": parsed.page_count,
                # Diagnostics only: how many pages needed OCR is logged, and a
                # page read by OCR is a page worth distrusting a little more.
                "extraction": page.source,
            },
        )
