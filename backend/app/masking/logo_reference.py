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


def _normalize_for_hash(im):
    """Flatten transparency onto neutral gray and trim uniform-color borders
    before hashing, so the SAME logo mark hashes close to identically
    regardless of what canvas it happens to sit on. Two real failure modes
    this closes: (1) flattening onto white/black would make a light- or
    dark-on-transparent logo variant vanish into the flatten color instead of
    staying visible; neutral gray keeps both visible. (2) without trimming,
    the same mark placed on a white background vs. a grey background (a
    common re-export difference, not a different logo) pushes the Hamming
    distance well past the match threshold, since phash is computed over the
    whole canvas including the background fill - trimming to the content
    bounding box before hashing removes that background as a variable."""
    from PIL import Image, ImageChops

    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, (128, 128, 128))
        flattened.paste(rgba, mask=rgba.split()[-1])
        im = flattened
    elif im.mode != "RGB":
        im = im.convert("RGB")

    bg = Image.new("RGB", im.size, im.getpixel((0, 0)))
    diff = ImageChops.difference(im, bg)
    bbox = diff.getbbox()
    return im.crop(bbox) if bbox else im


def compute_phash(image_bytes: bytes) -> str | None:
    """Perceptual hash of raster image bytes, or None if it can't be opened
    (corrupt/truncated/unsupported - callers must degrade gracefully, not fail
    the whole scan over one bad image)."""
    try:
        import imagehash
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            im.load()
            normalized = _normalize_for_hash(im)
        return str(imagehash.phash(normalized))
    except Exception:
        return None


def phash_distance(a: str, b: str) -> int | None:
    """Hamming distance between two hex phashes; None if either won't parse."""
    try:
        import imagehash

        # ImageHash.__sub__ returns numpy.int64, not a plain int - left as-is,
        # this poisons json.dumps() the moment it reaches output_json (a
        # numpy scalar looks and prints like an int but isn't JSON-serializable).
        return int(imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b))
    except Exception:
        return None


_distance = phash_distance


def load_all_references(db: Session) -> list[LogoReference]:
    """Fetch the full reference table once. find_matches() is called once
    PER IMAGE GROUP in a document (image_scan.py's scan_document_images) -
    for a chart-heavy or logo-heavy deck with dozens of distinct images,
    calling db.query(LogoReference).all() from inside find_matches itself
    would re-fetch and re-scan the ENTIRE table once per image, an O(images
    x references) query pattern for what is otherwise a cheap in-memory
    Hamming-distance comparison. Callers that process many images in one
    run should fetch once with this and pass the result to find_matches
    via `references`, instead of leaving it to re-query every time."""
    return db.query(LogoReference).all()


def find_matches(
    db: Session, phash: str, threshold: int = UNCERTAIN_THRESHOLD, references: list[LogoReference] | None = None
) -> list[tuple[uuid.UUID, int]]:
    """Every stored reference within `threshold` Hamming distance, as
    (mask_entity_id, distance), closest first. Empty if no phash or no hit.
    Pass `references` (from load_all_references) to reuse an already-fetched
    reference set across many calls in the same run rather than re-querying
    the table each time; omitted, it queries fresh (unchanged behavior for
    any single-shot caller)."""
    if not phash:
        return []
    if references is None:
        references = db.query(LogoReference).all()
    hits: list[tuple[uuid.UUID, int]] = []
    for ref in references:
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
