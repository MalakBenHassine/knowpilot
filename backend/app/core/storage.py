"""Where the uploaded bytes live.

Files go to a directory, metadata goes to PostgreSQL. Keeping multi-megabyte
blobs out of the database keeps backups fast and the working set small, and it
leaves one seam to change the day this moves to an object store.

Everything an attacker controls - the filename, the declared type, the declared
size - is treated as a claim to verify, never as a fact.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

# 20 MB: large enough for a scanned contract, small enough that one upload
# cannot fill a free VM or hold a worker for minutes.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# Read in 64 KB pieces. A 20 MB file never sits in memory as one block, so ten
# simultaneous uploads cost kilobytes instead of hundreds of megabytes.
READ_CHUNK_BYTES = 64 * 1024

PDF_MAGIC = b"%PDF-"
# Enough to recognise a file; also what the type sniffer looks at.
SNIFF_BYTES = 2048


class StorageError(Exception):
    """Base class for every refusal at the storage boundary."""


class FileTooLargeError(StorageError):
    """More bytes than MAX_UPLOAD_BYTES."""


class EmptyFileError(StorageError):
    """Zero bytes: nothing to parse, nothing to index."""


@dataclass(frozen=True)
class StoredFile:
    path: str
    size_bytes: int
    # SHA-256 of the content. Two identical files always produce it, whatever
    # they were named, which is what makes deduplication possible.
    content_hash: str


def detect_mime_type(head: bytes) -> str | None:
    """Recognise a file from its first bytes, never from its extension.

    A file called report.pdf can hold anything at all. The extension is a hint
    chosen by whoever uploads, so it is worth exactly nothing; the content is
    the only thing that cannot lie.

    Returns None when the type is not one we accept, which the API turns into
    415.
    """
    if head.startswith(PDF_MAGIC):
        return "application/pdf"

    # A NUL byte never appears in text and is the cheapest binary tell.
    if b"\x00" in head:
        return None

    # The sample can cut a multi-byte character in half, so drop up to three
    # trailing bytes before deciding the file is not UTF-8.
    for trim in range(4):
        candidate = head[: len(head) - trim] if trim else head
        try:
            candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
        return "text/plain"

    return None


class FileStorage:
    """Stores file contents under one root directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def path_for(self, document_id: uuid.UUID) -> Path:
        """Build the path of a document. No user input reaches it.

        The name is the identifier we generated, sharded on its first two
        characters so a single directory never holds a hundred thousand
        entries. Because the name the user chose never touches the filesystem,
        a filename like ../../etc/passwd is not merely blocked - it cannot be
        expressed.
        """
        name = str(document_id)
        return self._root / name[:2] / name

    async def save(self, stream: AsyncIterator[bytes], document_id: uuid.UUID) -> StoredFile:
        """Write a stream to disk, hashing and measuring it on the way.

        Reading and hashing in one pass avoids walking 20 MB twice.
        """
        path = self.path_for(document_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        digest = hashlib.sha256()
        size = 0

        try:
            # Blocking writes of 64 KB land in the page cache and return in
            # microseconds. Worth revisiting only if the limit grows a lot.
            with path.open("wb") as file:
                async for piece in stream:
                    size += len(piece)
                    # Checked while writing, never against Content-Length: the
                    # client controls that header and can simply lie about it.
                    if size > MAX_UPLOAD_BYTES:
                        raise FileTooLargeError(f"over {MAX_UPLOAD_BYTES} bytes")
                    digest.update(piece)
                    file.write(piece)

            if size == 0:
                raise EmptyFileError("the uploaded file is empty")
        except StorageError:
            # Never leave a half-written file behind: it would count against
            # the disk for ever and belongs to no document row.
            path.unlink(missing_ok=True)
            raise

        return StoredFile(path=str(path), size_bytes=size, content_hash=digest.hexdigest())

    def delete(self, document_id: uuid.UUID) -> None:
        """Remove the bytes. Missing is success: deleting twice must be safe."""
        self.path_for(document_id).unlink(missing_ok=True)
