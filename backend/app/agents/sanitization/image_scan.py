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
from app.documents.images import ImageRef, extract_images, guess_image_format
from app.llm import bedrock_client
from app.masking import dictionary
from app.masking.dictionary import is_own_firm
from app.masking.logo_reference import (
    MATCH_THRESHOLD,
    UNCERTAIN_THRESHOLD,
    compute_phash,
    find_matches,
    is_own_firm_logo,
    load_all_references,
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


_VISION_MAX_DIM = 4096  # comfortably under Bedrock's accepted dimensions
_VISION_MIN_DIM = 32  # tiny spacer/tracking-pixel-style images get upscaled, not rejected


def _normalize_for_vision(data: bytes) -> tuple[bytes, str] | None:
    """Round-trip every raster image through Pillow before it ever reaches
    Bedrock, even ones already in a nominally Bedrock-native format. Converse
    vision rejects images for reasons the raw bytes never reveal locally -
    CMYK-encoded JPEGs, indexed/16-bit-depth PNGs, animated GIFs, embedded ICC
    profiles, or images only a few pixels wide - all of which pass the local
    magic-byte sniff just fine and then fail server-side with
    "ValidationException: ... Could not process image". Re-encoding to a
    plain RGB PNG - alpha flattened onto neutral gray (not white/black, so a
    light- or dark-on-transparent logo variant stays visible instead of
    vanishing into the flatten color, which was also making the vision model
    misjudge some reversed-color logos as "blank") and clamped to a sane size
    range - sidesteps this whole class of "technically the right format,
    still rejected" failures. Returns None if Pillow itself can't open the
    bytes; the caller falls back to the pre-normalization bytes rather than
    failing the scan outright."""
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return None

    if getattr(im, "is_animated", False):
        im.seek(0)

    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, (128, 128, 128))
        flattened.paste(rgba, mask=rgba.split()[-1])
        im = flattened
    elif im.mode != "RGB":
        im = im.convert("RGB")

    if im.width > _VISION_MAX_DIM or im.height > _VISION_MAX_DIM:
        im.thumbnail((_VISION_MAX_DIM, _VISION_MAX_DIM), Image.LANCZOS)
    elif im.width < _VISION_MIN_DIM or im.height < _VISION_MIN_DIM:
        scale = max(_VISION_MIN_DIM / im.width, _VISION_MIN_DIM / im.height)
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)

    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue(), "png"


def _raster_bytes(ref: ImageRef) -> tuple[bytes | None, str | None]:
    """Bytes Bedrock vision / imagehash can actually open, and their real
    format. Anything already in a Bedrock-native format (png/jpeg/gif/webp)
    passes through unchanged; everything else - historically-"vector" formats
    (EMF/WMF/SVG) AND other embedded raster formats Bedrock simply doesn't
    accept (BMP/TIFF are the common ones Office embeds) - is rasterized to
    real PNG via LibreOffice, the same conversion path vectors already used.
    Previously only VECTOR_FORMATS were routed through this conversion, so a
    perfectly good BMP/TIFF logo fell through unconverted: correctly
    identified as BMP/TIFF (or, worse, not recognized at all) by format-
    sniffing, and never sent to Bedrock - flagged as an unsupported format
    instead of actually being scanned. Returns (None, None) if rasterization
    itself fails; the caller must still flag this for human review rather
    than fail outright.

    Every path additionally goes through _normalize_for_vision() - see there
    for why "already a supported format" isn't the same as "actually
    processable by Bedrock". If THAT fails too, this returns (None, None)
    rather than falling back to the un-normalized bytes: Pillow can open
    essentially any valid png/jpeg/gif/webp (and anything LibreOffice just
    rasterized), so a failure at that point means the bytes are genuinely
    corrupt/truncated, not merely "a format Bedrock happens to dislike".
    Sending them anyway is exactly what used to reproduce the same
    "ValidationException: ... Could not process image" on every single scan
    of that image - falling back to raw bytes here silently re-introduced
    the very failure mode the format-sniffing/rasterization short-circuit
    above already exists to prevent."""
    real_format = guess_image_format(ref.image_bytes, fallback=None) or ref.image_format
    if real_format in _VISION_FORMATS:
        data = ref.image_bytes
    else:
        try:
            from app.documents.convert import rasterize_image_to_png

            data = rasterize_image_to_png(ref.image_bytes, real_format)
        except Exception:
            return None, None

    return _normalize_for_vision(data) or (None, None)


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
    db: Session,
    g_idx: int,
    occurrences: list[ImageRef],
    raster: bytes | None,
    raster_format: str | None,
    phash: str | None,
    logo_references: list | None = None,
) -> tuple[ImageGroup, int, int, float]:
    sample = occurrences[0]
    locations = sorted({o.location_label for o in occurrences})
    all_indices = [o.index for o in occurrences]

    logo_hits = find_matches(db, phash, threshold=UNCERTAIN_THRESHOLD, references=logo_references) if phash else []
    best_logo = logo_hits[0] if logo_hits else None

    # _raster_bytes() guarantees raster_format is a Bedrock-native format
    # whenever raster is non-None (it either passed the bytes through
    # unchanged because they already were, or rasterized to real PNG) - so
    # "could not get usable bytes at all" and "got bytes but in an
    # unsupported format" collapse into this single case.
    if raster is None or raster_format is None:
        return (
            ImageGroup(
                group_index=g_idx, sample_ref=sample, locations=locations, all_indices=all_indices,
                contains_client_identity=best_logo is not None and best_logo[1] <= MATCH_THRESHOLD,
                description="could not render this image to a scannable format - flagged for manual review",
                confidence=0.0, phash=phash,
                logo_match_entity_id=best_logo[0] if best_logo else None,
                logo_match_distance=best_logo[1] if best_logo else None,
                needs_human_judgment=True,
            ),
            0, 0, 0.0,
        )

    try:
        resp = await bedrock_client.converse_vision(
            system_prompt=_system_prompt(),
            user_message="Does this image reveal which client this document is for?",
            image_bytes=raster,
            image_format=raster_format,
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
# variant, each previously costing its own Bedrock vision call (and, worse,
# showing the reviewer the same logo as several separate cards with an
# undercounted occurrence count each). Reuse MATCH_THRESHOLD - the same "this
# is confidently the same image" bar already proven out for CROSS-document
# logo matching (app.masking.logo_reference) - rather than a stricter,
# arbitrary number: two renditions of one logo within a single document are
# at least as similar as two renditions across different documents, so
# there's no principled reason to require a tighter match here.
PERCEPTUAL_DEDUP_THRESHOLD = MATCH_THRESHOLD


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
        raster, raster_format = _raster_bytes(occurrences[0])
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
            clusters.append({"occurrences": list(occurrences), "raster": raster, "raster_format": raster_format, "phash": phash})

    skipped = max(0, len(clusters) - MAX_IMAGES_SCANNED)
    to_scan = clusters[:MAX_IMAGES_SCANNED]

    groups: list[ImageGroup] = []
    total_in = total_out = 0
    total_cost = 0.0

    # Fetched once per document rather than once per image: find_matches()
    # only needs a fresh Hamming-distance comparison against each reference,
    # not a fresh query - a chart-heavy or logo-heavy deck could otherwise
    # re-run the same full-table scan dozens of times in one run.
    logo_references = load_all_references(db)

    for g_idx, cluster in enumerate(to_scan):
        group, in_tok, out_tok, cost = await _scan_one_group(
            db, g_idx, cluster["occurrences"], cluster["raster"], cluster["raster_format"], cluster["phash"],
            logo_references=logo_references,
        )
        groups.append(group)
        total_in += in_tok
        total_out += out_tok
        total_cost += cost

    groups = _merge_visually_similar_groups(groups)
    return groups, total_in, total_out, total_cost, skipped


def _normalized_brand_surface(surface: str | None) -> str | None:
    """Case/whitespace-insensitive key for comparing two OCR-matched brand
    strings - "Johnson & Johnson" and "johnson  &  johnson" must compare
    equal, since they're the same brand read off two different renditions of
    its logo. None if there's nothing to compare."""
    if not surface:
        return None
    normalized = " ".join(surface.strip().split()).casefold()
    return normalized or None


def _merge_visually_similar_groups(groups: list[ImageGroup]) -> list[ImageGroup]:
    """Post-scan consolidation, deliberately separate from (and looser than)
    the pre-scan PERCEPTUAL_DEDUP_THRESHOLD clustering above. Pre-scan
    clustering has to stay tight - it decides what to spend a vision call on
    BEFORE knowing what anything is, so merging too eagerly there risks
    silently treating two genuinely different logos as one. Post-scan, we
    already have real results in hand, so we can afford to be more generous,
    and to use signals beyond raw pixel similarity - two renditions of the
    same brand's logo can legitimately differ enough in color scheme or
    background fill to sit outside any phash threshold, and are still one
    logo, not two:
      1. Phash distance within UNCERTAIN_THRESHOLD - the same "likely
         related" bar already trusted for cross-document logo matching -
         catches the same logo at a different crop/resolution/compression
         (observed: a dedicated case-study slide's large rendition of a
         client wordmark vs. a small icon inside a client-logo grid
         elsewhere in the same deck, which OCR failed to read on the
         smaller one).
      2. Same OCR-matched brand text - if two separately-scanned images both
         deterministically OCR to the same known name/entity, they're the
         same brand regardless of how different the pixels look (a white-on-
         transparent vs. a full-color rendition of one company's wordmark,
         for instance).
      3. Same logo_match_entity_id - both images independently phash-matched
         the SAME stored reference logo. Hamming distance obeys the triangle
         inequality, so two renditions can each sit within threshold of that
         shared reference while being further apart from each other than the
         reference-to-reference bar alone would allow - matching the same
         known entity is still authoritative.
    Left unmerged, the reviewer sees one logo as several separate cards, each
    under-reporting its own occurrence count.
    Union-find over a small list (bounded by MAX_IMAGES_SCANNED) - plain O(n^2)
    pairwise comparison is cheap at this size."""
    if len(groups) < 2:
        return groups

    parent = list(range(len(groups)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            gi, gj = groups[i], groups[j]

            same_phash = False
            if gi.phash is not None and gj.phash is not None:
                d = phash_distance(gi.phash, gj.phash)
                same_phash = d is not None and d <= UNCERTAIN_THRESHOLD

            same_logo_entity = (
                gi.logo_match_entity_id is not None and gi.logo_match_entity_id == gj.logo_match_entity_id
            )

            brand_i, brand_j = _normalized_brand_surface(gi.ocr_matched_surface), _normalized_brand_surface(gj.ocr_matched_surface)
            same_ocr_brand = brand_i is not None and brand_i == brand_j

            if same_phash or same_logo_entity or same_ocr_brand:
                union(i, j)

    clusters: dict[int, list[ImageGroup]] = {}
    for i, g in enumerate(groups):
        clusters.setdefault(find(i), []).append(g)

    merged: list[ImageGroup] = []
    for members in clusters.values():
        if len(members) == 1:
            merged.append(members[0])
            continue
        primary = max(members, key=lambda g: g.confidence)
        logo_matches = [(g.logo_match_entity_id, g.logo_match_distance) for g in members if g.logo_match_entity_id is not None]
        best_logo_entity_id, best_logo_distance = min(logo_matches, key=lambda t: t[1]) if logo_matches else (None, None)
        merged.append(ImageGroup(
            group_index=primary.group_index,
            sample_ref=primary.sample_ref,
            locations=sorted({loc for g in members for loc in g.locations}),
            all_indices=sorted({i for g in members for i in g.all_indices}),
            contains_client_identity=any(g.contains_client_identity for g in members),
            description=primary.description or next((g.description for g in members if g.description), ""),
            confidence=max(g.confidence for g in members),
            ocr_text=list(dict.fromkeys(s for g in members for s in g.ocr_text)),
            ocr_matched_surface=next((g.ocr_matched_surface for g in members if g.ocr_matched_surface), None),
            logo_match_entity_id=best_logo_entity_id,
            logo_match_distance=best_logo_distance,
            needs_human_judgment=any(g.needs_human_judgment for g in members),
            phash=primary.phash,
        ))

    # Renumber sequentially - agent.py's proposal payload and the reviewer's
    # include/exclude edits are keyed by group_index, so downstream code must
    # only ever see the final, already-merged list.
    for new_idx, g in enumerate(merged):
        g.group_index = new_idx
    return merged


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
