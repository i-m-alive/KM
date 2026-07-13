"""Scans embedded images for client-identifying content (logos, screenshots,
letterhead) via Bedrock vision - the one thing text extraction can never see.

Three independent signals are combined per image, because each catches a
different failure mode a client logo can hide behind:
  1. Vision judgment - "does this look like it reveals the client" (existing).
  2. OCR - literal text strings the model can read off the image, run through
     the SAME deterministic detectors (masking dictionary + regex) already
     used for document text. Catches wordmarks even when the vision model's
     own semantic judgment is uncertain.
  3. Perceptual-hash logo matching against previously-confirmed client logos
     (app.masking.logo_reference) - catches ICON-ONLY marks with no readable
     text at all, which OCR structurally cannot catch.
A confident hit from (2) or (3) overrides the vision model's own verdict,
same principle as the masking dictionary short-circuiting text detection.

Groups occurrences by content hash first, so a logo repeated on every slide is
scanned once, not once per occurrence.
"""

import hashlib
import io
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.agents.sanitization.ner_prepass import regex_candidates_for_text
from app.config import get_settings
from app.documents.images import VECTOR_FORMATS, ImageRef, extract_images
from app.llm import bedrock_client
from app.masking import dictionary
from app.masking.dictionary import is_own_firm
from app.masking.logo_reference import (
    MATCH_THRESHOLD,
    UNCERTAIN_THRESHOLD,
    compute_phash,
    find_matches,
    is_own_firm_logo,
    phash_distance,
)

settings = get_settings()

_VISION_FORMATS = {"png", "jpeg", "jpg", "gif", "webp"}

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "contains_client_identity": {"type": "boolean"},
        "description": {"type": "string"},
        "confidence": {"type": "number"},
        "ocr_text": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["contains_client_identity", "description", "confidence", "ocr_text"],
}

def _system_prompt() -> str:
    # Naming the delivery firm explicitly (rather than only saying "our own
    # firm's branding" in the abstract) is what actually lets the model apply
    # the exclusion - it has no other way to know which name in the deck is
    # "us" vs. "the client". This mirrors the deterministic is_own_firm() check
    # applied to OCR/text entities, so vision judgment and the hard override
    # agree instead of one silently overriding the other after the fact.
    own_firm = ", ".join(f"'{n}'" for n in settings.OWN_FIRM_NAMES if n.strip())
    own_firm_clause = (
        f"Our own delivery firm is named {own_firm} (and close variants/spellings of it) - "
        f"this is NEVER the client, do not flag it or transcribe it as revealing client identity, "
        f"even in a 'who we are' self-introduction slide. "
        if own_firm
        else ""
    )
    return (
        "You are the Sanitization agent's image scanner. Look at this image from an engagement document "
        "and decide whether it reveals WHICH client the work was for: a company logo, letterhead, branding, "
        "a screenshot of the client's own software with their name/branding visible, or named individuals "
        "identifiable as client staff (e.g. a business card, an email signature). "
        f"{own_firm_clause}"
        "Do NOT flag: generic charts/diagrams/icons, our own firm's branding, stock photography, or screenshots "
        "with no client name/logo visible. Also transcribe every literal string of text visible anywhere in the "
        "image (wordmarks, captions, labels) into ocr_text, even short fragments - list each as its own entry, "
        "empty list if none. Give a confidence (0-1)."
    )


@dataclass
class ImageGroup:
    group_index: int
    sample_ref: ImageRef
    locations: list[str]
    all_indices: list[int]  # every ImageRef.index sharing this hash (for redaction)
    contains_client_identity: bool
    description: str
    confidence: float
    ocr_text: list[str] = field(default_factory=list)
    ocr_matched_surface: str | None = None  # a known/regex-detected surface found in ocr_text, if any
    logo_match_entity_id: object = None  # uuid.UUID | None
    logo_match_distance: int | None = None
    needs_human_judgment: bool = False
    phash: str | None = None


# Real decks with broadened enumeration (masters/layouts/headers/footers/
# thumbnails, not just in-slide pictures) can easily carry 50-100+ unique
# media parts. A cap this low left the majority of a real client deck's
# images unscanned - meaning most of it was never actually verifiable at all,
# not just unlikely to contain a logo. Raised to keep near-total coverage
# for realistic decks; still bounded so a single pathological document can't
# run an unbounded number of vision calls.
MAX_IMAGES_SCANNED = 150


def _raster_bytes(ref: ImageRef) -> bytes | None:
    """Bytes Bedrock vision / imagehash can actually open. Vector formats need
    rasterizing first (LibreOffice); if that fails, we degrade to no
    OCR/vision/phash for this image rather than failing the whole scan -
    callers must still flag it for human review since it went unscanned."""
    if ref.image_format not in VECTOR_FORMATS:
        return ref.image_bytes
    try:
        from app.documents.convert import rasterize_image_to_png

        return rasterize_image_to_png(ref.image_bytes, ref.image_format)
    except Exception:
        return None


def _ocr_match(ocr_text: list[str], db: Session) -> str | None:
    """First OCR'd string that resolves to a known dictionary entity or trips
    a regex identifier - the same deterministic signal document text gets."""
    for s in ocr_text:
        s = s.strip()
        if not s or is_own_firm(s):
            continue
        if dictionary.lookup(db, s) is not None:
            return s
        if regex_candidates_for_text(s):
            return s
    return None


async def _scan_one_group(
    db: Session, g_idx: int, occurrences: list[ImageRef], raster: bytes | None, phash: str | None
) -> tuple[ImageGroup, int, int, float]:
    sample = occurrences[0]
    locations = sorted({o.location_label for o in occurrences})
    all_indices = [o.index for o in occurrences]

    if raster is None:
        return (
            ImageGroup(
                group_index=g_idx, sample_ref=sample, locations=locations, all_indices=all_indices,
                contains_client_identity=False, description="could not render this image format for scanning",
                confidence=0.0, needs_human_judgment=True,
            ),
            0, 0, 0.0,
        )

    logo_hits = find_matches(db, phash, threshold=UNCERTAIN_THRESHOLD) if phash else []
    best_logo = logo_hits[0] if logo_hits else None

    try:
        resp = await bedrock_client.converse_vision(
            system_prompt=_system_prompt(),
            user_message="Does this image reveal which client this document is for?",
            image_bytes=raster,
            image_format="png",
            response_schema=VISION_SCHEMA,
        )
    except Exception as exc:
        group = ImageGroup(
            group_index=g_idx, sample_ref=sample, locations=locations, all_indices=all_indices,
            contains_client_identity=best_logo is not None and best_logo[1] <= MATCH_THRESHOLD,
            description=f"scan failed: {exc}", confidence=0.0, phash=phash,
            logo_match_entity_id=best_logo[0] if best_logo else None,
            logo_match_distance=best_logo[1] if best_logo else None,
            needs_human_judgment=best_logo is not None and best_logo[1] > MATCH_THRESHOLD,
        )
        return group, 0, 0, 0.0

    parsed = resp.parsed or {}
    ocr_text = [s for s in (parsed.get("ocr_text") or []) if isinstance(s, str)]
    ocr_matched = _ocr_match(ocr_text, db)
    vision_flag = bool(parsed.get("contains_client_identity", False))
    confidence = float(parsed.get("confidence", 0.0))

    logo_confident = best_logo is not None and best_logo[1] <= MATCH_THRESHOLD
    logo_uncertain = best_logo is not None and MATCH_THRESHOLD < best_logo[1] <= UNCERTAIN_THRESHOLD

    # A confident deterministic signal (known-entity OCR match, or a confident
    # logo match) overrides the vision model's own uncertain judgment - same
    # short-circuit principle as the masking dictionary for text.
    contains_client_identity = vision_flag or ocr_matched is not None or logo_confident
    if ocr_matched is not None or logo_confident:
        confidence = max(confidence, 0.95)

    # Hard override, not just a prompt instruction: the vision model's own
    # contains_client_identity flag is observed to sometimes disagree with
    # its own description text (e.g. writing "this does NOT reveal client
    # identity" while still setting the flag true) - own-firm names are
    # NEVER the client, full stop, so a deterministic OCR hit here wins over
    # whatever the model's boolean said, the same way is_own_firm already
    # gates every other entry point (text detection, dictionary merge).
    # Perceptual-hash matching is a SECOND, independent own-firm signal for
    # when OCR itself is unreliable (observed: a stylized wordmark flagged
    # contains_client_identity=True at 99% confidence with empty ocr_text -
    # nothing for the text-based check above to match against).
    own_firm_hit = any(is_own_firm(s) for s in ocr_text) or is_own_firm_logo(phash)
    if own_firm_hit:
        contains_client_identity = False

    # Recall honesty: don't silently pass an image whose only signal is a
    # borderline logo match, or a vision call that landed in an uncertain
    # confidence band with nothing else corroborating it.
    needs_human_judgment = not own_firm_hit and (
        logo_uncertain or (0.3 <= confidence <= 0.7 and not contains_client_identity)
    )

    description = parsed.get("description", "")
    if contains_client_identity and not own_firm_hit and (ocr_matched is not None or logo_confident):
        # The vision model's own free-text description is observed to
        # sometimes hedge or flatly contradict its own signals (e.g. calling
        # a known, previously-confirmed client logo "internal branding, not
        # client identification") - exactly the wording that once talked a
        # reviewer into excluding a real leak. A confident deterministic
        # match (known OCR/dictionary hit, or a confident logo-hash match)
        # is authoritative; make that the HEADLINE so it can't be missed or
        # out-argued by the model's own uncertain commentary underneath.
        reason = f"OCR text '{ocr_matched}'" if ocr_matched is not None else "logo-hash match to a known entity"
        description = f"CONFIDENT MATCH ({reason}) - recommend redaction regardless of the note below. Model's note: {description or '(none)'}"

    group = ImageGroup(
        group_index=g_idx, sample_ref=sample, locations=locations, all_indices=all_indices,
        contains_client_identity=contains_client_identity,
        description=description, confidence=confidence,
        ocr_text=ocr_text, ocr_matched_surface=ocr_matched, phash=phash,
        logo_match_entity_id=best_logo[0] if best_logo else None,
        logo_match_distance=best_logo[1] if best_logo else None,
        needs_human_judgment=needs_human_judgment,
    )
    return group, resp.input_tokens, resp.output_tokens, resp.estimated_cost_usd


# Two SHA-distinct images within this phash Hamming distance are treated as
# THE SAME image for scanning purposes - the same logo re-exported at a
# different compression/resize shows up as a different SHA-256 on every slide
# variant, each previously costing its own Bedrock vision call. Kept tight
# (well under MATCH_THRESHOLD) so only near-identical renditions merge, never
# two genuinely different logos.
PERCEPTUAL_DEDUP_THRESHOLD = 2


async def scan_document_images(
    stored_path: str, content_type: str, filename: str, db: Session
) -> tuple[list[ImageGroup], int, int, float, int]:
    """Returns (groups, input_tokens, output_tokens, cost, skipped_count)."""
    refs = extract_images(stored_path, content_type, filename)

    by_hash: dict[str, list[ImageRef]] = {}
    for ref in refs:
        h = hashlib.sha256(ref.image_bytes).hexdigest()
        by_hash.setdefault(h, []).append(ref)

    # Second-stage perceptual dedup: cluster SHA-unique groups whose phashes
    # are near-identical, so one vision call covers every rendition and an
    # approval on the cluster redacts ALL of them (all_indices aggregates
    # across the merged SHA groups). Unhashable images stay singletons.
    clusters: list[dict] = []
    for occurrences in by_hash.values():
        raster = _raster_bytes(occurrences[0])
        phash = compute_phash(raster) if raster is not None else None
        merged = False
        if phash is not None:
            for c in clusters:
                if c["phash"] is None:
                    continue
                d = phash_distance(phash, c["phash"])
                if d is not None and d <= PERCEPTUAL_DEDUP_THRESHOLD:
                    c["occurrences"].extend(occurrences)
                    merged = True
                    break
        if not merged:
            clusters.append({"occurrences": list(occurrences), "raster": raster, "phash": phash})

    skipped = max(0, len(clusters) - MAX_IMAGES_SCANNED)
    to_scan = clusters[:MAX_IMAGES_SCANNED]

    groups: list[ImageGroup] = []
    total_in = total_out = 0
    total_cost = 0.0

    for g_idx, cluster in enumerate(to_scan):
        group, in_tok, out_tok, cost = await _scan_one_group(
            db, g_idx, cluster["occurrences"], cluster["raster"], cluster["phash"]
        )
        groups.append(group)
        total_in += in_tok
        total_out += out_tok
        total_cost += cost

    return groups, total_in, total_out, total_cost, skipped


def _is_own_placeholder(g: "ImageGroup") -> bool:
    """True if this group IS our own synthetic [REDACTED] gray box, not real
    content. Re-scanning the rendered file means the vision model sometimes
    sees this placeholder and hedges ("could be a brand identifier if this
    were real") - a self-inflicted false positive on our own redaction
    marker, not a genuine residual leak.

    OCR text is the first signal, but not reliable alone: at small sizes or
    low contrast the model sometimes fails to read the "REDACTED" label at
    all (empty ocr_text) while still flagging the box as suspicious on shape/
    color alone. Falling back to recognizing our own KNOWN, DETERMINISTIC
    pixel content (a near-solid (60,60,60) gray box - see
    image_redact.placeholder_png) catches that case without depending on the
    model reading anything."""
    from app.documents.image_redact import PLACEHOLDER_LABEL, is_placeholder_bytes

    ocr_normalized = {s.strip().upper() for s in g.ocr_text}
    if ocr_normalized and ocr_normalized.issubset({PLACEHOLDER_LABEL}):
        return True
    return is_placeholder_bytes(g.sample_ref.image_bytes)


async def find_residual_image_groups(
    masked_path: str, content_type: str, filename: str, db: Session
) -> tuple[list[ImageGroup], int, int, int, float]:
    """Re-run the full scan against the RENDERED masked file and return the
    groups that still look client-identifying, as STRUCTURED data (locations
    + all_indices survive into output_json so a later remediation pass can
    target exactly these images without re-scanning anything). Returns
    (residual_groups, skipped, input_tokens, output_tokens, cost)."""
    groups, in_tok, out_tok, cost, skipped = await scan_document_images(masked_path, content_type, filename, db)
    residual = [g for g in groups if g.contains_client_identity and not _is_own_placeholder(g)]
    return residual, skipped, in_tok, out_tok, cost


def residual_image_messages(residual_groups: list[ImageGroup], skipped: int) -> list[str]:
    """Human-readable flag lines for the structured residuals."""
    messages = []
    for g in residual_groups:
        detail = (
            g.description
            or g.ocr_matched_surface
            or (f"OCR read: {', '.join(g.ocr_text)}" if g.ocr_text else None)
            or f"flagged at {g.confidence:.0%} confidence, no description or OCR text returned - inspect this image manually"
        )
        messages.append(f"{g.locations[0] if g.locations else 'image'} (group {g.group_index}): {detail}")
    if skipped:
        messages.append(f"{skipped} image(s) could not be re-verified (scan cap reached)")
    return messages


async def find_residual_images(masked_path: str, content_type: str, filename: str, db: Session) -> list[str]:
    """Message-only form of find_residual_image_groups, kept for callers that
    just need flag text - it must re-derive the verdict from scratch rather
    than trusting anything computed pre-render."""
    residual_groups, skipped, _, _, _ = await find_residual_image_groups(masked_path, content_type, filename, db)
    return residual_image_messages(residual_groups, skipped)
