"""Targeted remediation of a completed_with_issues Sanitization run.

Instead of re-running the whole pipeline on the original document, this
operates on the run's ALREADY-RENDERED masked output and fixes only what the
verification flags said is still dirty:

- Flagged images are redacted in place, scoped to exactly the slides/pages
  the verify scan identified. The apply-phase verify persists structured
  residuals (extraction indices into the masked file) in output_json, so for
  new runs this needs ZERO new vision calls; older runs without that data
  fall back to one fresh scan of the masked file. Redaction is a media-part
  swap (docx/pptx/xlsx) or per-page redaction rects (PDF) - the document
  "reassembles" itself because everything else in the file is untouched.
- Metadata / comment / hyperlink residuals are re-scrubbed (idempotent).
- Every channel is then re-verified: the deterministic channels from scratch,
  and images by confirming that none of the targeted images' original bytes
  survive anywhere in the re-extracted file (works uniformly: the OOXML swap
  replaces the bytes, PDF redaction removes the image entirely).

Text residuals can NOT be fixed here - text masking happens during rendering
from the original, so a text-channel failure still requires a full re-run.
"""

import hashlib
import os
from datetime import datetime

from sqlalchemy.orm import Session

from app.agents.sanitization.image_scan import find_residual_image_groups
from app.documents.comment_scan import find_residual_comments
from app.documents.comment_scrub import scrub_comments
from app.documents.hyperlink_scan import find_residual_hyperlinks
from app.documents.hyperlink_scrub import scrub_hyperlinks
from app.documents.image_redact import is_placeholder_bytes, redact_images
from app.documents.images import extract_images
from app.documents.metadata_scan import find_residual_metadata
from app.documents.metadata_scrub import scrub_metadata
from app.documents.verify import find_residual_surfaces
from app.models import AgentRun, MaskingEntity, MaskingOccurrence, RunFlag, RunStep, UploadedDocument


class RemediationError(ValueError):
    """A precondition failed - surfaced to the API caller as a 400/409."""


def _surface_to_token_for_run(db: Session, run: AgentRun) -> dict[str, str]:
    """Rebuild the exact mask map this run applied, from its persisted
    occurrences - output_json deliberately carries no client surfaces."""
    mapping: dict[str, str] = {}
    occurrences = db.query(MaskingOccurrence).filter(MaskingOccurrence.run_id == run.id).all()
    for occ in occurrences:
        if occ.entity_id is None or occ.surface_text in mapping:
            continue
        entity = db.get(MaskingEntity, occ.entity_id)
        if entity is not None:
            mapping[occ.surface_text] = entity.mask_token
    return mapping


def _remove_resolved_flags(db: Session, run: AgentRun, channels_now_clean: list[str], images_clean: bool) -> int:
    """Drop the blocking flags that described the now-fixed state - leaving
    them would keep telling the user the file is unsafe after it verified
    clean. Info/warning flags (low-confidence skips etc.) are history worth
    keeping and stay untouched."""
    removed = 0
    for flag in list(run.flags):
        if flag.severity != "blocking":
            continue
        is_channel_flag = any(f"Verification failed ({ch})" in flag.message for ch in channels_now_clean)
        is_detect_image_flag = images_clean and "appear to reveal the client" in flag.message
        if is_channel_flag or is_detect_image_flag:
            db.delete(flag)
            removed += 1
    return removed


async def remediate_run(db: Session, run: AgentRun) -> dict:
    output = run.output_json if isinstance(run.output_json, dict) else {}
    masked_path = output.get("masked_document_path")
    if run.agent_id != "sanitization":
        raise RemediationError("Only Sanitization runs can be remediated")
    if run.status != "completed_with_issues":
        raise RemediationError(f"Run is '{run.status}' - only completed_with_issues runs can be remediated")
    if not masked_path or not os.path.exists(masked_path):
        raise RemediationError("This run's rendered masked file no longer exists on disk - re-run Sanitization on the original document instead")

    doc = db.get(UploadedDocument, output.get("document_id"))
    if doc is None:
        raise RemediationError("The run's source document record no longer exists")
    content_type, filename = doc.content_type, doc.filename
    masking_style = output.get("masking_style", "token")

    surface_to_token = _surface_to_token_for_run(db, run)
    surfaces = list(surface_to_token.keys())

    steps: list[tuple[str, str]] = []

    # --- 1. deterministic scrubs (idempotent - re-running on clean channels is a no-op)
    meta_fixed = scrub_metadata(masked_path, content_type, filename, surface_to_token, masking_style)
    links_fixed = scrub_hyperlinks(masked_path, content_type, filename, surface_to_token, masking_style)
    comments_fixed = scrub_comments(masked_path, content_type, filename, surface_to_token, masking_style)
    if meta_fixed or links_fixed or comments_fixed:
        steps.append((
            "remediate: re-scrub channels",
            f"{meta_fixed} metadata propert(ies), {links_fixed} hyperlink target(s), {comments_fixed} comment fragment(s) rewritten",
        ))

    # --- 2. images: resolve exactly what the last verification flagged
    all_refs = extract_images(masked_path, content_type, filename)
    by_index = {r.index: r for r in all_refs}
    persisted_groups = output.get("residual_image_groups") or []
    scan_in = scan_out = 0
    scan_cost = 0.0

    if persisted_groups:
        target_refs = [
            by_index[i]
            for g in persisted_groups
            for i in (g.get("all_indices") or [])
            if i in by_index
        ]
        flagged_locations = sorted({loc for g in persisted_groups for loc in (g.get("locations") or [])})
        scope_detail = (
            f"scoped to {len(persisted_groups)} flagged group(s) from the previous verification"
            f" ({', '.join(flagged_locations) or 'unknown locations'}) - no new vision scans needed"
        )
    else:
        # Older run without structured residuals: one fresh scan of the
        # masked output (deduped + placeholder-aware), then redact whatever
        # it still flags.
        residual_groups, _, scan_in, scan_out, scan_cost = await find_residual_image_groups(
            masked_path, content_type, filename, db
        )
        target_refs = [by_index[i] for g in residual_groups for i in g.all_indices if i in by_index]
        scope_detail = f"no stored residual data on this run - re-scanned the masked file and found {len(residual_groups)} flagged group(s)"

    # Skip anything already swapped for our placeholder (double-click safety).
    target_refs = [r for r in target_refs if not is_placeholder_bytes(r.image_bytes)]
    target_hashes = {hashlib.sha256(r.image_bytes).hexdigest() for r in target_refs}

    images_redacted = images_unlocated = 0
    if target_refs:
        images_redacted, images_unlocated = redact_images(masked_path, content_type, filename, target_refs)
    steps.append((
        "remediate: redact flagged images",
        f"{scope_detail}; {len(target_refs)} occurrence(s) targeted, {images_redacted} redacted"
        + (f", {images_unlocated} could not be located (PDF pattern-fill)" if images_unlocated else ""),
    ))

    # --- 3. re-verify
    residual_text = find_residual_surfaces(masked_path, content_type, filename, surfaces)
    residual_metadata = find_residual_metadata(masked_path, content_type, filename, surfaces)
    residual_comments = find_residual_comments(masked_path, content_type, filename, surfaces)
    residual_hyperlinks = find_residual_hyperlinks(masked_path, content_type, filename, surfaces)

    # Images verify deterministically: none of the targeted images' original
    # bytes may survive anywhere in the re-extracted file. (OOXML swap
    # replaces the bytes; PDF redaction removes the image - both make the
    # original hash disappear.) Everything else in the file was untouched,
    # so the untargeted images' verdict from the last full scan still holds.
    surviving = [
        r.location_label
        for r in extract_images(masked_path, content_type, filename)
        if hashlib.sha256(r.image_bytes).hexdigest() in target_hashes
    ]
    verified_images = not surviving and images_unlocated == 0

    verified = {
        "text": len(residual_text) == 0,
        "images": verified_images,
        "metadata": len(residual_metadata) == 0,
        "comments": len(residual_comments) == 0,
        "hyperlinks": len(residual_hyperlinks) == 0,
    }
    native_masking_verified = all(verified.values())
    steps.append((
        "remediate: re-verify masking (text/images/metadata/comments/hyperlinks)",
        "clean across all channels" if native_masking_verified else "one or more channels still expose a masked term",
    ))

    # --- 4. persist: output, flags, steps, status
    channels_clean = [ch for ch, ok in verified.items() if ok]
    _remove_resolved_flags(db, run, channels_clean, images_clean=verified["images"])

    new_flags: list[tuple[str, str]] = []
    for ch, residual in (
        ("text", residual_text), ("metadata", residual_metadata),
        ("comments", residual_comments), ("hyperlinks", residual_hyperlinks),
    ):
        if residual:
            new_flags.append((
                "blocking",
                f"Verification failed ({ch}) after remediation: {len(residual)} item(s) - {', '.join(residual[:5])}. "
                + ("A text residual cannot be fixed in place - re-run Sanitization on the original document." if ch == "text" else "Do not distribute this file as-is."),
            ))
    if not verified["images"]:
        detail = f"{len(surviving)} targeted image(s) still present ({', '.join(surviving[:5])})" if surviving else f"{images_unlocated} image(s) could not be located on the page"
        new_flags.append(("blocking", f"Verification failed (images) after remediation: {detail}. Do not distribute this file as-is."))
    new_flags.append((
        "info",
        f"Remediation pass: {images_redacted} image(s) redacted in place, "
        f"{meta_fixed + links_fixed + comments_fixed} metadata/hyperlink/comment fragment(s) re-scrubbed. "
        + ("All channels now verify clean." if native_masking_verified else "Issues remain - see the flags above."),
    ))

    existing_steps = db.query(RunStep).filter(RunStep.run_id == run.id).count()
    for offset, (name, detail) in enumerate(steps, start=1):
        db.add(RunStep(run_id=run.id, step_order=existing_steps + offset, name=name, detail=detail, tool=None))
    for severity, message in new_flags:
        db.add(RunFlag(run_id=run.id, message=message, severity=severity))

    run.output_json = {
        **output,
        "native_masking_verified": native_masking_verified,
        "verified_text": verified["text"],
        "verified_images": verified["images"],
        "verified_metadata": verified["metadata"],
        "verified_comments": verified["comments"],
        "verified_hyperlinks": verified["hyperlinks"],
        "images_redacted": int(output.get("images_redacted") or 0) + images_redacted,
        "residual_image_groups": [] if verified["images"] else persisted_groups,
        "remediated_at": datetime.utcnow().isoformat(),
    }
    run.input_tokens = (run.input_tokens or 0) + scan_in
    run.output_tokens = (run.output_tokens or 0) + scan_out
    run.estimated_cost_usd = float(run.estimated_cost_usd or 0) + scan_cost
    run.status = "completed" if native_masking_verified else "completed_with_issues"
    db.commit()

    # Remediation is a second path to "completed" alongside the normal
    # apply()->_finalize_completed flow - it must trigger the SAME auto-chain
    # hook, or a document fixed via remediation (rather than approved clean
    # on the first pass) would silently never hand off to Tagging under
    # Coordinator-started runs.
    from app.runs.background import _maybe_auto_chain_to_tagging

    _maybe_auto_chain_to_tagging(db, run)

    return {
        "native_masking_verified": native_masking_verified,
        "images_redacted": images_redacted,
        "channels": verified,
    }
