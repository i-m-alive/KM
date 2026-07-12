import io
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.permissions import has_capability, require_capability
from app.db import get_db
from app.documents import storage
from app.documents.convert import ConversionUnavailableError, to_pdf_cached
from app.documents.extract import UnsupportedDocumentError, extract_chunks
from app.documents.images import extract_images
from app.models import UploadedDocument, User
from app.schemas import DocumentOut

router = APIRouter(prefix="/documents", tags=["documents"])


def _content_type_for(fmt: str) -> str:
    return {
        "png": "image/png",
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(fmt.lower(), "application/octet-stream")


def _authorize_document(doc: UploadedDocument, user: User) -> None:
    if doc.uploaded_by != user.id and not has_capability(user, "review_queue_manage"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed to access this document")


@router.post("", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    # Uploading a document = submitting it into the pipeline.
    user: User = Depends(require_capability("submit_documents")),
    db: Session = Depends(get_db),
) -> DocumentOut:
    filename = file.filename or "upload"
    content_type = file.content_type or "application/octet-stream"

    if not storage.is_supported(filename, content_type):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only PDF, DOCX, PPTX, and XLSX are supported")

    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty file")

    document_id = uuid.uuid4()
    stored_path = storage.save_upload(document_id, filename, data)

    # Validate we can actually extract it before recording the row.
    try:
        chunks = extract_chunks(stored_path, content_type, filename)
    except UnsupportedDocumentError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Could not read document: {exc}") from exc

    doc = UploadedDocument(
        id=document_id,
        filename=filename,
        content_type=content_type,
        stored_path=stored_path,
        uploaded_by=user.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    return DocumentOut(
        id=doc.id,
        filename=doc.filename,
        content_type=doc.content_type,
        uploaded_at=doc.uploaded_at,
        chunk_count=len(chunks),
    )


@router.get("", response_model=list[DocumentOut])
def list_documents(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[DocumentOut]:
    docs = (
        db.query(UploadedDocument)
        .filter(UploadedDocument.uploaded_by == user.id)
        .order_by(UploadedDocument.uploaded_at.desc())
        .all()
    )
    return [
        DocumentOut(
            id=d.id, filename=d.filename, content_type=d.content_type, uploaded_at=d.uploaded_at, chunk_count=None
        )
        for d in docs
    ]


@router.get("/{document_id}/images/{index}")
def get_document_image(
    document_id: uuid.UUID, index: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Thumbnail for an embedded image (by stable index), used by the reviewer
    UI to show the image the vision scan flagged."""
    doc = db.get(UploadedDocument, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    _authorize_document(doc, user)

    refs = extract_images(doc.stored_path, doc.content_type, doc.filename)
    ref = next((r for r in refs if r.index == index), None)
    if ref is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found")

    return StreamingResponse(io.BytesIO(ref.image_bytes), media_type=_content_type_for(ref.image_format))


@router.get("/{document_id}/preview")
def preview_original_document(
    document_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """PDF preview of the ORIGINAL uploaded document (converted via LibreOffice
    if it isn't already a PDF), for side-by-side comparison against the masked
    version."""
    doc = db.get(UploadedDocument, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    _authorize_document(doc, user)

    try:
        pdf_path = to_pdf_cached(doc.stored_path)
    except ConversionUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    return FileResponse(pdf_path, media_type="application/pdf")
