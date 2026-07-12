import os
import uuid

from app.config import get_settings

settings = get_settings()

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx"}


def is_supported(filename: str, content_type: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return content_type in ALLOWED_CONTENT_TYPES or ext in ALLOWED_EXTENSIONS


def save_upload(document_id: uuid.UUID, filename: str, data: bytes) -> str:
    """Persist raw upload bytes under uploads/<document_id><ext>, return the path."""
    os.makedirs(settings.UPLOADS_DIR, exist_ok=True)
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    stored_path = os.path.join(settings.UPLOADS_DIR, f"{document_id}{ext}")
    with open(stored_path, "wb") as f:
        f.write(data)
    return stored_path
