"""Document endpoints.

Every handler reads the owner from the session, never from the request. A
document identifier in a URL is a claim made by whoever sends it; the session
cookie is the only thing we issued ourselves.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Response, UploadFile, status
from sqlalchemy.exc import IntegrityError

from app.api.deps import CsrfProtected, CurrentSession, Db, Ingestion, Model, Storage
from app.core.storage import (
    READ_CHUNK_BYTES,
    SNIFF_BYTES,
    EmptyFileError,
    FileStorage,
    FileTooLargeError,
    StoredFile,
    detect_mime_type,
    safe_filename,
)
from app.db import documents as repository
from app.db import vector_store
from app.rag.pipeline import ingest_document
from app.schemas.document import DocumentListResponse, DocumentResponse

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("", summary="List the documents of the current user")
async def list_documents(session: CurrentSession, database: Db) -> DocumentListResponse:
    """Newest first. An empty library is an empty list, not a 404."""
    # `sub` is the Keycloak subject: stable even if the user changes email.
    found = await repository.list_documents(database, owner_id=session.sub)
    return DocumentListResponse(items=[DocumentResponse.of(document) for document in found])


async def _body(upload: UploadFile, head: bytes) -> AsyncIterator[bytes]:
    """Replay the bytes already read, then stream the rest."""
    yield head
    while piece := await upload.read(READ_CHUNK_BYTES):
        yield piece


async def _store_upload(
    storage: FileStorage, upload: UploadFile, head: bytes, document_id: uuid.UUID
) -> StoredFile:
    try:
        return await storage.save(_body(upload, head), document_id)
    except FileTooLargeError as error:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large") from error
    except EmptyFileError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The file is empty") from error


@router.post("", status_code=status.HTTP_201_CREATED, summary="Upload a document")
async def upload_document(
    session: CsrfProtected,
    database: Db,
    storage: Storage,
    model: Model,
    store: Ingestion,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File()],
) -> DocumentResponse:
    """Accept a file, store it, and index it in the background.

    Returns 201 as soon as the bytes are safe, without waiting for indexing:
    embedding a long document takes a minute, and a request held open that long
    would be killed by every proxy between here and the browser. The document
    comes back as `processing`, and the interface follows the real stage.
    """
    # 1. Recognise the type from the first bytes, before anything is written.
    #    A refused upload must not cost us a single write: that is both faster
    #    and one less way to fill the disk.
    head = await file.read(SNIFF_BYTES)
    if not head:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The file is empty")

    mime_type = detect_mime_type(head)
    if mime_type is None:
        # 415, not 400: the request is well formed, we simply do not read that
        # kind of file. The extension is never consulted.
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Unsupported file type")

    # 2. The identifier is generated here and used as the filename on disk, so
    #    the name the user chose never touches the filesystem.
    document_id = uuid.uuid4()
    stored = await _store_upload(storage, file, head, document_id)

    try:
        # 3. Same file, same user, already here: refuse rather than index the
        #    same passages twice and pay for the embeddings again.
        existing = await repository.find_by_content_hash(
            database, owner_id=session.sub, content_hash=stored.content_hash
        )
        if existing is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "This document has already been uploaded")

        document = await repository.create_document(
            database,
            document_id=document_id,
            owner_id=session.sub,
            filename=safe_filename(file.filename),
            mime_type=mime_type,
            size_bytes=stored.size_bytes,
            content_hash=stored.content_hash,
            storage_path=stored.path,
        )
    except IntegrityError as error:
        # 4. Two identical uploads at the same instant both pass the check
        #    above: checking and inserting are not one atomic act. The UNIQUE
        #    constraint is, so we let the database arbitrate and translate its
        #    verdict into the same 409.
        storage.delete(document_id)
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This document has already been uploaded"
        ) from error
    except Exception:
        # 5. The bytes are on disk but no row will ever point at them. Delete
        #    them now: an orphaned file is invisible, counts against the disk
        #    for ever, and nothing will ever come looking for it.
        storage.delete(document_id)
        raise

    # 6. Scheduled after the response, and given its own persistence: by then
    #    the request session is closed and this transaction is committed.
    background.add_task(
        ingest_document,
        store,
        model,
        document_id=document_id,
        owner_id=session.sub,
    )

    return DocumentResponse.of(document)


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document",
)
async def delete_document(
    session: CsrfProtected,
    database: Db,
    storage: Storage,
    background: BackgroundTasks,
    document_id: uuid.UUID,
) -> Response:
    """Remove a document, its passages and its bytes.

    404 both when it does not exist and when it belongs to somebody else: a 403
    would confirm the identifier is real, which is enough to enumerate accounts.
    """
    deleted = await vector_store.delete_document(
        database, owner_id=session.sub, document_id=document_id
    )
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    # The chunks are gone with the row, by ON DELETE CASCADE, in this very
    # transaction. The file cannot join that transaction - a filesystem has no
    # rollback - so it is removed AFTER the response, which means after the
    # commit. Deleting it here would risk erasing the bytes of a document whose
    # deletion was then rolled back, leaving a row pointing at nothing.
    background.add_task(storage.delete, document_id)

    # 204: there is nothing sensible to return, and an empty body is not JSON.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{document_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry a failed ingestion",
)
async def retry_document(
    session: CsrfProtected,
    database: Db,
    model: Model,
    store: Ingestion,
    background: BackgroundTasks,
    document_id: uuid.UUID,
) -> DocumentResponse:
    """Run the pipeline again on a document that failed for a passing reason.

    202 Accepted, not 200: the work is scheduled, not done. The document comes
    back as `processing` and the interface follows the stage as before.
    """
    document = await repository.get_document(
        database, owner_id=session.sub, document_id=document_id
    )
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    if document.status != "failed" or not document.retryable:
        # 409, not 404: the document exists and belongs to the caller, the
        # request simply makes no sense for its current state. A scanned page
        # will not become readable because it is asked twice.
        raise HTTPException(status.HTTP_409_CONFLICT, "This document cannot be retried")

    await repository.reset_for_retry(database, document_id=document_id)
    background.add_task(
        ingest_document,
        store,
        model,
        document_id=document_id,
        owner_id=session.sub,
    )
    return DocumentResponse.of(document)
