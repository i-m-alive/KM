"""Perceptual-hash logo matching - PROTECTED calls (agent code only).

Closes the "icon-only mark, no readable text" gap OCR can't catch. Reference
hashes are never manually curated; they're written once, automatically, when
a reviewer approves an image redaction (see agent.py apply()), keyed to the
same canonical entity as that image's text mask token if one was resolved.
"""

import io
import uuid

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import LogoReference

settings = get_settings()

MATCH_THRESHOLD = 4  # Hamming distance <= this = confident match
UNCERTAIN_THRESHOLD = 10  # <= this (but > MATCH_THRESHOLD) = needs_human_judgment


def compute_phash(image_bytes: bytes) -> str | None:
    """Perceptual hash of raster image bytes, or None if it can't be opened
    (corrupt/truncated/unsupported - callers must degrade gracefully, not fail
    the whole scan over one bad image)."""
    try:
        import imagehash
        from PIL import Image

        return str(imagehash.phash(Image.open(io.BytesIO(image_bytes))))
    except Exception:
        return None


def _distance(a: str, b: str) -> int | None:
    try:
        import imagehash

        # ImageHash.__sub__ returns numpy.int64, not a plain int - left as-is,
        # this poisons json.dumps() the moment it reaches output_json (a
        # numpy scalar looks and prints like an int but isn't JSON-serializable).
        return int(imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b))
    except Exception:
        return None


def find_matches(db: Session, phash: str, threshold: int = UNCERTAIN_THRESHOLD) -> list[tuple[uuid.UUID, int]]:
    """Every stored reference within `threshold` Hamming distance, as
    (mask_entity_id, distance), closest first. Empty if no phash or no hit."""
    if not phash:
        return []
    hits: list[tuple[uuid.UUID, int]] = []
    for ref in db.query(LogoReference).all():
        d = _distance(phash, ref.phash)
        if d is not None and d <= threshold:
            hits.append((ref.mask_entity_id, d))
    hits.sort(key=lambda t: t[1])
    return hits


def store_reference(db: Session, entity_id: uuid.UUID, phash: str, run_id: uuid.UUID | None) -> None:
    if not phash:
        return
    db.add(LogoReference(mask_entity_id=entity_id, phash=phash, source_run_id=run_id))
    db.flush()


def is_own_firm_logo(phash: str | None, threshold: int = MATCH_THRESHOLD) -> bool:
    """A second, OCR-independent signal for the own-firm exclusion (see
    settings.OWN_FIRM_LOGO_PHASHES) - needed because vision-model OCR
    transcription of a logo's text is unreliable; a stylized wordmark can be
    correctly flagged as "reveals identity" at high confidence while ocr_text
    comes back empty, giving the text-based own-firm check nothing to match."""
    if not phash:
        return False
    return any(
        (d := _distance(phash, known)) is not None and d <= threshold
        for known in settings.OWN_FIRM_LOGO_PHASHES
        if known.strip()
    )
