"""Perceptual-hash logo matching - PROTECTED calls (agent code only).

Closes the "icon-only mark, no readable text" gap OCR can't catch. Reference
hashes are never manually curated; they're written once, automatically, when
a reviewer approves an image redaction (see agent.py apply()), keyed to the
same canonical entity as that image's text mask token if one was resolved.
"""

import io
import os
import uuid

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import LogoReference

settings = get_settings()

MATCH_THRESHOLD = 4  # Hamming distance <= this = confident match
UNCERTAIN_THRESHOLD = 10  # <= this (but > MATCH_THRESHOLD) = needs_human_judgment

_THUMBNAIL_MAX_SIZE = (160, 160)


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


def build_band_index(references: list[LogoReference]) -> dict[tuple[int, str], list[LogoReference]]:
    """LSH-style band index over a reference set - one bucket per (hex
    position, character) pair of each reference's phash. A phash from
    imagehash.phash() is a 64-bit hash written as 16 hex digits; splitting it
    into its 16 single-digit bands means that, by pigeonhole, any two hashes
    with Hamming distance < 16 are GUARANTEED to share at least one identical
    band - safely covering both MATCH_THRESHOLD (4) and UNCERTAIN_THRESHOLD
    (10) with zero false negatives. Bands only PRUNE the candidate set before
    the real, exact Hamming-distance check in find_matches() - they never
    decide a match themselves, so this can't silently loosen what counts as
    a match, only cut how many references get compared.

    Fine to skip at today's scale (a full scan is cheap under a few hundred
    references - see find_matches' docstring); this exists so that scale
    doesn't require revisiting the matching logic itself, only building this
    index once per run/document, the same way load_all_references() is
    already built once and reused across every image group's find_matches
    call rather than re-querying per image."""
    index: dict[tuple[int, str], list[LogoReference]] = {}
    for ref in references:
        if not ref.phash:
            continue
        for pos, ch in enumerate(ref.phash):
            index.setdefault((pos, ch), []).append(ref)
    return index


def _candidates_from_index(band_index: dict, phash: str) -> list[LogoReference]:
    seen_ids: set = set()
    candidates: list[LogoReference] = []
    for pos, ch in enumerate(phash):
        for ref in band_index.get((pos, ch), ()):
            if ref.id not in seen_ids:
                seen_ids.add(ref.id)
                candidates.append(ref)
    return candidates


def find_matches(
    db: Session,
    phash: str,
    threshold: int = UNCERTAIN_THRESHOLD,
    references: list[LogoReference] | None = None,
    band_index: dict | None = None,
) -> list[tuple[uuid.UUID, int]]:
    """Every stored reference within `threshold` Hamming distance, as
    (mask_entity_id, distance), closest first. Empty if no phash or no hit.

    Three ways to source the candidate set, in priority order:
    - `band_index` (from build_band_index): prunes to just the references
      sharing a band with `phash` before the exact distance check - the
      LSH-style path, for when the reference table has grown past a size
      where a full scan per image is worth avoiding.
    - `references` (from load_all_references): reuses an already-fetched
      reference set across many calls in the same run, full O(n) scan.
    - neither: queries fresh (unchanged behavior for any single-shot caller).

    Every path still computes the exact Hamming distance and applies
    `threshold` itself - band_index changes only how many references get
    compared, never what counts as a match."""
    if not phash:
        return []
    if band_index is not None:
        candidates = _candidates_from_index(band_index, phash)
    elif references is not None:
        candidates = references
    else:
        candidates = db.query(LogoReference).all()
    hits: list[tuple[uuid.UUID, int]] = []
    for ref in candidates:
        d = _distance(phash, ref.phash)
        if d is not None and d <= threshold:
            hits.append((ref.mask_entity_id, d))
    hits.sort(key=lambda t: t[1])
    return hits


def _save_thumbnail(entity_id: uuid.UUID, image_bytes: bytes) -> str | None:
    """Best-effort small PNG preview of the approved image, so governance can
    SEE what a mask token actually matched instead of only a hex phash - None
    if the bytes can't be opened (unsupported/corrupt format), same
    degrade-gracefully contract as compute_phash. Filename includes a short
    byte-content hash (not the phash) so two different renditions of the same
    logo don't collide on disk before each gets its own LogoReference row."""
    try:
        import hashlib

        from PIL import Image

        out_dir = os.path.join(settings.OUTPUTS_DIR, "logo_thumbnails")
        os.makedirs(out_dir, exist_ok=True)
        digest = hashlib.sha256(image_bytes).hexdigest()[:16]
        path = os.path.join(out_dir, f"{entity_id}__{digest}.png")
        with Image.open(io.BytesIO(image_bytes)) as im:
            im.load()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.mode or im.mode == "P" else "RGB")
            im.thumbnail(_THUMBNAIL_MAX_SIZE)
            im.save(path, format="PNG")
        return path
    except Exception:
        return None


def store_reference(
    db: Session, entity_id: uuid.UUID, phash: str, run_id: uuid.UUID | None, image_bytes: bytes | None = None,
) -> None:
    if not phash:
        return
    thumbnail_path = _save_thumbnail(entity_id, image_bytes) if image_bytes else None
    db.add(LogoReference(mask_entity_id=entity_id, phash=phash, source_run_id=run_id, thumbnail_path=thumbnail_path))
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
