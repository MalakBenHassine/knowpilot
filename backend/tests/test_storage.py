"""File storage and type detection.

Everything here runs on a temporary directory: no database, no network.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest

from app.core.storage import (
    EmptyFileError,
    FileStorage,
    FileTooLargeError,
    detect_mime_type,
    safe_filename,
)


async def stream(*pieces: bytes) -> AsyncIterator[bytes]:
    for piece in pieces:
        yield piece


def save(storage: FileStorage, document_id: uuid.UUID, *pieces: bytes):  # type: ignore[no-untyped-def]
    return anyio.run(lambda: storage.save(stream(*pieces), document_id))


# --- Type detection --------------------------------------------------------


def test_a_pdf_is_recognised_by_its_content() -> None:
    assert detect_mime_type(b"%PDF-1.7\n1 0 obj") == "application/pdf"


def test_utf8_text_is_recognised() -> None:
    # b"\xc3\xa9" is the letter e with an acute accent in UTF-8.
    assert detect_mime_type(b"Une note en fran\xc3\xa7ais.") == "text/plain"


def test_a_truncated_multibyte_character_does_not_fool_the_sniffer() -> None:
    # The sample ends on a lone \xc3, the first half of an accented letter cut
    # by the 2048-byte window. A naive decode would read that as binary and
    # reject a perfectly valid text file.
    assert detect_mime_type(b"Un caf\xc3\xa9 a Paris, un resum\xc3") == "text/plain"


def test_a_file_named_pdf_but_holding_a_zip_is_refused() -> None:
    # The extension is chosen by whoever uploads. Only the content is evidence.
    assert detect_mime_type(b"PK\x03\x04\x14\x00\x00\x00") is None


def test_binary_content_is_refused() -> None:
    assert detect_mime_type(b"\x89PNG\r\n\x1a\n\x00\x00") is None


def test_binary_without_a_single_nul_byte_is_still_refused() -> None:
    # Found by the API test, not by this file: my first sniffer only looked for
    # NUL, and a ZIP header holds none. PK\x03\x04 decodes as valid UTF-8, so
    # an archive was being accepted as a text document. A convenient sample -
    # one that happened to contain NUL - had hidden the gap.
    assert detect_mime_type(b"PK\x03\x04binary") is None


# --- Writing files ---------------------------------------------------------


def test_a_file_is_written_hashed_and_measured(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    document_id = uuid.uuid4()

    stored = save(storage, document_id, b"hello ", b"world")

    assert Path(stored.path).read_bytes() == b"hello world"
    assert stored.size_bytes == 11
    # SHA-256 of "hello world", so two identical uploads are recognisable.
    assert stored.content_hash == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )


def test_the_name_chosen_by_the_user_never_reaches_the_filesystem(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path)
    document_id = uuid.uuid4()

    stored = save(storage, document_id, b"content")

    # The path is built only from the identifier we generated, so a filename
    # like ../../etc/passwd is not blocked - it is never used at all.
    path = Path(stored.path).resolve()
    assert path.is_relative_to(tmp_path.resolve())
    assert path.name == str(document_id)
    # Sharded, so one directory never holds a hundred thousand files.
    assert path.parent.name == str(document_id)[:2]


def test_an_oversized_upload_is_refused_and_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.core.storage.MAX_UPLOAD_BYTES", 10)
    storage = FileStorage(tmp_path)
    document_id = uuid.uuid4()

    with pytest.raises(FileTooLargeError):
        save(storage, document_id, b"12345", b"67890", b"overflow")

    # The limit is enforced while writing, not from Content-Length, and the
    # partial file is removed: otherwise it would eat disk for ever while
    # belonging to no document row.
    assert not storage.path_for(document_id).exists()


def test_an_empty_upload_is_refused(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    document_id = uuid.uuid4()

    with pytest.raises(EmptyFileError):
        save(storage, document_id, b"")

    assert not storage.path_for(document_id).exists()


def test_deleting_is_safe_to_repeat(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    document_id = uuid.uuid4()
    save(storage, document_id, b"content")

    storage.delete(document_id)
    # A cleanup task may run twice; the second run must not raise.
    storage.delete(document_id)

    assert not storage.path_for(document_id).exists()


# --- Filenames -------------------------------------------------------------


def test_a_filename_keeps_only_its_last_segment() -> None:
    # Never used on disk, but written to logs and rendered in the interface.
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:/Users/malak/contract.pdf") == "contract.pdf"


def test_control_characters_are_removed_from_a_filename() -> None:
    # A newline inside a name lets an attacker forge a second, entirely fake
    # line in the logs. It is called log injection, and it is how an intrusion
    # gets buried under plausible noise.
    cleaned = safe_filename("report.pdf\n2026-09-18 INFO user admin logged in")

    assert "\n" not in cleaned
    assert cleaned.isprintable()


def test_an_absurdly_long_filename_is_cut_to_the_column_size() -> None:
    # Refused at the boundary rather than by the database, halfway through a
    # transaction that has already written a file to disk.
    assert len(safe_filename("a" * 10_000)) == 255


def test_a_missing_or_blank_filename_still_yields_something_displayable() -> None:
    assert safe_filename(None) == "document"
    assert safe_filename("   ") == "document"
    assert safe_filename("../..") == "document"
