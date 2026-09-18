"""Document endpoints.

Every handler reads the owner from the session, never from the request. A
document identifier in a URL is a claim by whoever sends it; the session is the
only thing we issued ourselves.
"""

from fastapi import APIRouter

from app.api.deps import CurrentSession, Db
from app.db import documents as repository
from app.schemas.document import DocumentListResponse, DocumentResponse

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("", summary="List the documents of the current user")
async def list_documents(session: CurrentSession, database: Db) -> DocumentListResponse:
    """Newest first. An empty library is an empty list, not a 404."""
    # `sub` is the Keycloak subject: stable even if the user changes email.
    found = await repository.list_documents(database, owner_id=session.sub)
    return DocumentListResponse(items=[DocumentResponse.of(document) for document in found])
