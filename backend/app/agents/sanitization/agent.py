"""A-01 Sanitization — the orchestrating background agent.

detect(): NER pre-pass -> deterministic dictionary pass -> LLM Detector (MCP
tool-use loop) -> assemble a proposal of what will be masked, file it for
review.  apply(): on approval, deterministically mask, persist the global
dictionary + occurrences, capture identity, summarize for Tagging.
"""

import re
import time
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.agents.base import AgentFlag, AgentResult, AgentStep, BackgroundAgent, ReviewProposal
from app.agents.sanitization import (
    alias_validate,
    detector,
    entity_actions,
    precision_check,
    reidentify,
    revalidate,
    review_deltas,
    sensitive_outcome,
    summarizer,
)
from app.agents.sanitization.apply_masks import apply_masks
from app.agents.sanitization.image_scan import (
    MAX_IMAGES_SCANNED,
    find_residual_image_groups,
    residual_image_messages,
    scan_document_images,
)
from app.agents.sanitization.ner_prepass import extract_candidates, presidio_available, regex_candidates_for_text
from app.agents.sanitization.regex_patterns import infra_credential
from app.config import get_settings
from app.documents.alttext_scan import extract_alt_text
from app.documents.alttext_scrub import scrub_alt_text
from app.documents.channel_coverage import audit_channel_coverage
from app.documents.comment_scan import find_residual_comments
from app.documents.comment_scrub import scrub_comments
from app.documents.exif_strip import strip_exif
from app.documents.extract import extract_chunks
from app.documents.hyperlink_scan import find_residual_hyperlinks
from app.documents.hyperlink_scrub import scrub_hyperlinks
from app.documents.image_redact import redact_images
from app.documents.images import extract_images
from app.documents.metadata_scan import find_residual_metadata
from app.documents.metadata_scrub import scrub_metadata
from app.documents.render import render_masked_document
from app.documents.verify import find_residual_surfaces
from app.masking import dictionary, registry
from app.masking.dictionary import is_own_firm
from app.masking.logo_reference import MATCH_THRESHOLD, store_reference
from app.masking.pattern import surface_pattern
from app.masking.style import resolve_style
from app.models import AgentRun, DocumentMetadata, MaskingEntity, MaskingOccurrence, ReviewItem, UploadedDocument
from app.storage.local_store import save_masked_document, save_run_output

settings = get_settings()


def _resolve_entity_inclusion(
    proposed_entities: list[dict], removed: set[str], included: set[str]
) -> tuple[list[dict], list[str]]:
    """The single place that decides which PROPOSED entities actually get
    masked, given the reviewer's edits and each entity's default_action
    (Phase 3's entity_actions.resolve_default_action, stamped onto every
    entity during detect()) - generalizes Phase 2's CREDENTIAL-only
    enforcement into all three defaults:

      "mandatory" - always kept, regardless of removed/included (CREDENTIAL,
                    unchanged from Phase 2 - a live credential should never
                    be reviewer-optional).
      "mask"      - kept unless the reviewer explicitly opted OUT via
                    removed_surfaces (Phase 1/2 behavior, unchanged).
      "flag"/"keep" - excluded UNLESS the reviewer explicitly opted IN via
                    included_surfaces - the new, opposite-default path for
                    COMMERCIAL_TERM/COMPETITOR_NAME/STRATEGY_MENTION/
                    OWN_COST_DETAIL/ORG_CHART_STRUCTURE and a non-consented
                    INTERNAL_TEAM_MEMBER.

    A proposal entity written before this field existed (defensive only -
    every entity detect() emits now sets it) falls back to "mask", the
    historical default. Returns (kept_entities, blocked_removal_surfaces) -
    the second is empty unless a removal was actually attempted against a
    mandatory entity, so callers can flag it without re-deriving which
    surfaces were blocked."""
    blocked = [
        e["surface_text"] for e in proposed_entities
        if e.get("default_action", "mask") == "mandatory" and e["surface_text"].lower() in removed
    ]
    kept = []
    for e in proposed_entities:
        action = e.get("default_action", "mask")
        surface_key = e["surface_text"].lower()
        if action == "mandatory":
            kept.append(e)
        elif action in ("flag", "keep"):
            if surface_key in included:
                kept.append(e)
        else:  # "mask"
            if surface_key not in removed:
                kept.append(e)
    return kept, blocked


def _apply_immediate_consent_grants(entities: list[dict], removed: set[str], consent_updates: dict) -> set[str]:
    """Returns `removed` with a fresh "granted" consent (edits.consent_updates)
    folded in for any INTERNAL_TEAM_MEMBER entity in `entities` - granting
    consent in THIS SAME submission must exempt this run's occurrence too,
    not only future ones. An entity's default_action was frozen at
    detect() time, before this consent existed, so
    _resolve_entity_inclusion alone would still mask someone the reviewer
    just explicitly cleared - without this, "grant consent" would silently
    do nothing until the NEXT run. Scoped to INTERNAL_TEAM_MEMBER only, so
    an unrelated surface can't be affected by an incidental key collision
    in consent_updates."""
    updated = set(removed)
    for e in entities:
        if e["entity_type"] == "INTERNAL_TEAM_MEMBER" and consent_updates.get(e["surface_text"]) == "granted":
            updated.add(e["surface_text"].lower())
    return updated


def _count_occurrences(surface: str, chunks) -> int:
    # Must use the SAME pattern apply_masks() actually masks with (word
    # boundaries + \s+ for wrapped whitespace) - a bare re.escape() literal
    # undercounts real occurrences whenever the surface is line-wrapped in
    # extracted text, which desyncs the reviewer-visible count from what
    # actually gets masked.
    pat = re.compile(surface_pattern(surface), re.IGNORECASE)
    return sum(len(pat.findall(c.text)) for c in chunks)


class SanitizationAgent(BackgroundAgent):
    agent_id = "sanitization"
    display_name = "Sanitization"
    description = (
        "Removes client-identifying information from a document using a global masking dictionary, "
        "then files the proposed masks for human review before applying them."
    )
    tools = ["bedrock", "presidio"]
    allowed_roles = ["admin", "km_governance", "km_reviewer", "practice_lead", "delivery"]

    async def detect(self, db: Session, run: AgentRun) -> ReviewProposal:
        steps: list[AgentStep] = []
        flags: list[AgentFlag] = []
        run.status = "detecting"
        db.commit()

        document_id = (run.input_json or {}).get("document_id")
        if not document_id:
            raise ValueError("input.document_id is required")
        doc = db.get(UploadedDocument, uuid.UUID(str(document_id)))
        if doc is None:
            raise ValueError(f"No document {document_id}")

        # Step 1: extract + NER pre-pass (free).
        t = time.monotonic()
        chunks = extract_chunks(doc.stored_path, doc.content_type, doc.filename)
        candidates = extract_candidates(chunks)
        # Alt-text (descr/title on cNvPr/docPr) sits in a seam between body
        # text and image pixels - extracted here, once, so it feeds BOTH the
        # dictionary full-text sweep below (known entities) and the
        # alt-text-specific merge after image scanning (brand-new ones).
        alt_texts = extract_alt_text(doc.stored_path, doc.content_type, doc.filename)
        steps.append(AgentStep(order=1, name="pre-pass", tool="presidio" if presidio_available() else "regex",
                               detail=f"{len(chunks)} chunks; {len(candidates)} distinct candidate strings; {len(alt_texts)} image alt-text value(s)",
                               duration_ms=int((time.monotonic() - t) * 1000)))

        # A scanned PDF (no text layer) makes the ENTIRE text channel blind -
        # extraction sees nothing, so detection, masking, and text
        # verification all trivially "pass" while every word on the page sits
        # in pixels. The image scan still covers it (each scanned page is one
        # big image), but the reviewer must know the text channel's green
        # checkmark means "nothing to check", not "checked and clean".
        is_pdf = doc.content_type == "application/pdf" or doc.filename.lower().endswith(".pdf")
        if is_pdf and chunks:
            total_text = sum(len(c.text.strip()) for c in chunks)
            if total_text < 50 * len(chunks):
                flags.append(AgentFlag(
                    message=(
                        f"This PDF has little or no extractable text ({total_text} chars across {len(chunks)} page(s)) - "
                        "likely a scan. The text channel cannot see or mask anything here; coverage relies entirely on "
                        "the image scan. Review the rendered output page by page before distributing."
                    ),
                    severity="warning",
                ))

        # Step 2: deterministic dictionary pass (known clients, free).
        # `known` maps the surface AS IT APPEARS IN THIS DOCUMENT -> entity;
        # apply() masks exactly that surface string, so it must be the form
        # actually present in the text, never just aliases[0] of the entity.
        t = time.monotonic()
        known: dict[str, object] = {}
        for c in candidates:
            entity = dictionary.lookup(db, c.surface_text)
            if entity is not None and entity.status == "approved" and not is_own_firm(c.surface_text):
                known[c.surface_text] = entity

        # Full-text sweep of the ENTIRE approved dictionary - the candidate
        # loop above only asks about strings the NER pre-pass happened to
        # surface, which made deterministic coverage hostage to that pass's
        # recall. Observed consequence: a weak LLM run proposed 3 entities
        # instead of the prior run's 11, and 6 already-APPROVED third-party
        # names (BlackRock, GSK, Siemens, ...) silently survived in a file
        # whose text channel still verified "clean", because verification
        # only checks proposed surfaces. Once an entity is approved in the
        # global dictionary, its masking must never again depend on any
        # per-run model behavior.
        full_text = "\n".join(c.text for c in chunks) + ("\n" + "\n".join(alt_texts) if alt_texts else "")
        swept = 0
        already = {e.mask_token for e in known.values()}
        for entity, matched_surface in dictionary.find_in_text(db, full_text):
            if entity.mask_token in already:
                continue
            known[matched_surface] = entity
            already.add(entity.mask_token)
            swept += 1
        steps.append(AgentStep(order=2, name="dictionary pass", tool="masking_dictionary",
                               detail=f"{len(known)} entit{'y' if len(known) == 1 else 'ies'} already known "
                                      f"({swept} via full-text dictionary sweep, beyond the candidate pass)",
                               duration_ms=int((time.monotonic() - t) * 1000)))

        # Step 3: LLM Detector via MCP tool-use loop. alt_texts passed as
        # context (not fs_read_document content) - see detector.py's
        # docstring for why: a name embedded in a messy alt-text phrase
        # (e.g. "GMR Group | Delhi") needs the model's real judgment, not
        # just the regex/dictionary substring checks in the merge below,
        # which can't reliably extract a clean proper noun from prose.
        t = time.monotonic()
        resp = await detector.detect_entities(str(document_id), len(chunks), candidates, alt_texts=alt_texts)
        llm_entities = (resp.parsed or {}).get("entities", [])
        steps.append(AgentStep(order=3, name="detect (LLM + MCP)", tool="bedrock",
                               detail=f"{len(llm_entities)} client entities proposed; {resp.input_tokens}+{resp.output_tokens} tok",
                               duration_ms=int((time.monotonic() - t) * 1000)))

        # Merge known (deterministic) + LLM entities, dedupe by normalized surface.
        merged: dict[str, dict] = {}
        for surface, entity in known.items():
            merged[dictionary.normalize(surface)] = {
                # The surface as found in THIS document (candidate string or
                # full-text-sweep match) - apply() masks exactly this string,
                # so aliases[0] (which may be a different variant of the same
                # entity) would mask the wrong form and miss the real one.
                "surface_text": surface,
                "entity_type": entity.entity_type,
                "confidence": 1.0,
                "known": True,
                "mask_token": entity.mask_token,
            }
        for e in llm_entities:
            surface = (e.get("surface_text") or "").strip()
            if not surface or is_own_firm(surface) or dictionary.is_skipped(db, surface):
                continue
            key = dictionary.normalize(surface)
            if key in merged:
                continue
            merged[key] = {
                "surface_text": surface,
                "entity_type": e.get("entity_type", "CLIENT_NAME"),
                "confidence": float(e.get("confidence", 0.5)),
                "known": False,
                "mask_token": None,
            }

        # Step 4: scan embedded images (logos, screenshots) via Bedrock vision -
        # text extraction above NEVER sees these; a client name baked into a
        # picture is invisible to every step before this one. This is also
        # where OCR/logo-match can surface a client name that appears NOWHERE
        # in text at all (the confirmed bug this closes): a wordmark logo.
        t = time.monotonic()
        image_groups, img_in, img_out, img_cost, skipped = await scan_document_images(doc.stored_path, doc.content_type, doc.filename, db, run_id=run.id)
        flagged_groups = [g for g in image_groups if g.contains_client_identity]
        needs_judgment_groups = [g for g in image_groups if g.needs_human_judgment]
        total_images = sum(len(g.all_indices) for g in image_groups) + skipped
        if total_images > 0:
            if flagged_groups:
                flags.append(AgentFlag(
                    message=f"{len(flagged_groups)} embedded image(s) appear to reveal the client (logo/screenshot) — review before treating this document as sanitized.",
                    severity="blocking",
                ))
            else:
                flags.append(AgentFlag(
                    message=f"{total_images} embedded image(s) found; none flagged as client-identifying, but images are not exhaustively verifiable — check manually.",
                    severity="info",
                ))
        if needs_judgment_groups:
            flags.append(AgentFlag(
                message=f"{len(needs_judgment_groups)} image(s) have an uncertain OCR/logo-match signal (stylized font, low-contrast mark, or borderline logo similarity) — a human needs to look, not a silent pass.",
                severity="warning",
            ))
        if skipped:
            flags.append(AgentFlag(message=f"{skipped} additional image(s) were not scanned (cap of {MAX_IMAGES_SCANNED}) — review manually.", severity="warning"))
        steps.append(AgentStep(order=4, name="scan images (vision + OCR + logo match)", tool="bedrock",
                               detail=f"{len(image_groups)} unique image(s) scanned; {len(flagged_groups)} flagged",
                               duration_ms=int((time.monotonic() - t) * 1000)))

        # Merge OCR-derived surfaces from client-identifying images into the
        # SAME entity pipeline as text - so a name read off a logo gets a
        # proper mask token, reviewer sign-off, and (via apply()) a logo
        # reference for future icon-only matches, exactly like any other entity.
        #
        # Also track how many times each such surface's SOURCE IMAGE recurs
        # (image_occurrences_by_key) - _count_occurrences below only ever
        # counts regex hits in extracted body TEXT, which is structurally
        # blind to pixel content. A name that's read off a logo and appears
        # nowhere in body text isn't a 0-occurrence entity, it's an
        # N-occurrence entity where the count lives in the image channel
        # instead of the text channel; reporting 0 there misleads the
        # reviewer into thinking nothing was actually found.
        image_occurrences_by_key: dict[str, int] = {}
        for g in image_groups:
            if not (g.contains_client_identity or g.needs_human_judgment):
                continue
            group_keys: set[str] = set()
            for s in g.ocr_text:
                s = s.strip()
                if len(s) < 2 or s.isdigit() or is_own_firm(s) or dictionary.is_skipped(db, s):
                    continue
                key = dictionary.normalize(s)
                group_keys.add(key)
                if key in merged:
                    continue
                # A logo-OCR'd fragment shorter than MIN_OCR_ENTITY_LENGTH is
                # too collision-prone with ordinary words/acronyms (e.g. "RIA",
                # "sure") to auto-trust at the image group's full confidence -
                # still surfaced for the reviewer, just capped below the
                # low-confidence bar so it can't be silently pre-approved.
                confidence = g.confidence
                if len(s) < settings.MIN_OCR_ENTITY_LENGTH:
                    # OCR-derived candidates are always typed CLIENT_NAME (line ~249 below).
                    confidence = min(confidence, settings.SANITIZATION_CONFIDENCE_THRESHOLDS.get("CLIENT_NAME", 0.6) - 0.01)
                merged[key] = {
                    "surface_text": s,
                    "entity_type": "CLIENT_NAME",
                    "confidence": confidence,
                    "known": False,
                    "mask_token": None,
                    "source": "image_ocr",
                }
            # Every distinct surface OCR'd off THIS image counts this image's
            # own occurrence total once - not once per OCR fragment, so three
            # strings read off the same picture don't triple-count it - and
            # accumulates across separate image groups that name the same
            # entity (the same client's logo appearing in two visually
            # different renditions elsewhere in the document).
            for key in group_keys:
                image_occurrences_by_key[key] = image_occurrences_by_key.get(key, 0) + len(g.all_indices)

        # Infrastructure & Security / Technical Diagrams (Phase 2): text read
        # off ANY image - not gated on contains_client_identity/
        # needs_human_judgment like the OCR merge above, since a hostname or
        # credential can appear in an otherwise unremarkable screenshot that
        # never trips the client-identity signal at all. Same merge-into-the-
        # same-entity-pipeline principle as OCR'd client names: a credential
        # read off a diagram gets a proper mask token, reviewer visibility,
        # and (via the mandatory_redaction flag below) the same
        # non-overridable treatment as one found in body text.
        sensitive_text_occurrences_by_key: dict[str, int] = {}
        credential_group_indices: set[int] = set()
        for g in image_groups:
            if not g.sensitive_text_matches:
                continue
            for surface, etype in g.sensitive_text_matches:
                if etype in infra_credential.CREDENTIAL_TYPES:
                    credential_group_indices.add(g.group_index)
                key = dictionary.normalize(surface)
                sensitive_text_occurrences_by_key[key] = sensitive_text_occurrences_by_key.get(key, 0) + len(g.all_indices)
                if key in merged:
                    continue
                merged[key] = {
                    "surface_text": surface, "entity_type": etype, "confidence": 0.9,
                    "known": False, "mask_token": None, "source": "image_ocr",
                }
        data_sample_groups = [g for g in image_groups if g.contains_real_data_sample]
        if credential_group_indices:
            flags.append(AgentFlag(
                message=f"{len(credential_group_indices)} embedded image(s) contain a possible credential (API key, token, or connection string) baked into the pixels — this will be masked unconditionally.",
                severity="blocking",
            ))
        if data_sample_groups:
            flags.append(AgentFlag(
                message=f"{len(data_sample_groups)} embedded image(s) appear to show real (non-synthetic) data — consider replacing with a synthetic/representative example before distributing.",
                severity="warning",
            ))

        # Alt-text (descr/title on cNvPr/docPr) - the same seam as image OCR,
        # but for text a producer TYPED rather than pixels a model transcribes.
        # Known/approved entities are already caught by the dictionary
        # full-text sweep above (full_text includes alt_texts); this
        # additionally surfaces brand-new, alt-text-ONLY strings using the
        # same deterministic regex/dictionary checks OCR text gets, PLUS a
        # dedicated LLM detector pass below (alt-text is often a messy
        # descriptive phrase like "GMR Group | Delhi" or "Bandhan Bank Vector
        # Logo Free Download", not a clean entity name - no zero-cost
        # deterministic method reliably extracts a proper noun out of that,
        # which is exactly why real names silently survived here before).
        #
        # find_in_text (substring sweep), NOT lookup (exact match): an
        # earlier version used lookup(db, s) - an EXACT match against the
        # WHOLE alt-text string - which was never going to fire for a known
        # entity embedded in a longer phrase ("NextCare" inside
        # "Nextcare_logo") the same way an exact dictionary lookup on a full
        # sentence never matches a name inside it.
        alt_text_occurrences_by_key: dict[str, int] = {}
        for s in alt_texts:
            s = s.strip()
            if len(s) < 2 or is_own_firm(s) or dictionary.is_skipped(db, s):
                continue
            key = dictionary.normalize(s)
            alt_text_occurrences_by_key[key] = alt_text_occurrences_by_key.get(key, 0) + 1
            if key in merged:
                continue
            known_hits = dictionary.find_in_text(db, s)
            if known_hits:
                for entity, matched_surface in known_hits:
                    ekey = dictionary.normalize(matched_surface)
                    if ekey in merged:
                        continue
                    merged[ekey] = {
                        "surface_text": matched_surface, "entity_type": entity.entity_type, "confidence": 1.0,
                        "known": True, "mask_token": entity.mask_token,
                    }
                continue
            regex_hits = regex_candidates_for_text(s)
            if regex_hits:
                surface, etype = regex_hits[0]
                if etype == "CLIENT_EMAIL_DOMAIN" and is_own_firm(surface):
                    continue
                rkey = dictionary.normalize(surface)
                if rkey not in merged:
                    merged[rkey] = {
                        "surface_text": surface, "entity_type": etype, "confidence": 0.9,
                        "known": False, "mask_token": None, "source": "alt_text",
                    }
                continue
            # No deterministic signal at all - surface the raw value, capped
            # just below the auto-include bar (same collision-avoidance
            # reasoning as MIN_OCR_ENTITY_LENGTH: an arbitrary descriptive
            # phrase is too noisy to auto-trust at full confidence, but must
            # still be VISIBLE rather than dropped). alt_text_llm_hits below
            # (when enabled) supersedes this with a real judgment call.
            merged[key] = {
                "surface_text": s, "entity_type": "CLIENT_NAME",
                "confidence": min(0.5, settings.SANITIZATION_CONFIDENCE_THRESHOLDS.get("CLIENT_NAME", 0.6) - 0.01),
                "known": False, "mask_token": None, "source": "alt_text",
            }

        # Below-threshold candidates are excluded from the auto-included
        # proposal, but NOT dropped entirely - they're still returned as
        # structured "excluded_entities" data so the reviewer can see and
        # include them with one click, rather than the previous behavior of
        # naming them only in a flag's free text and requiring the reviewer
        # to retype the exact surface string via "add entity" to recover one
        # (the actual cause of low-occurrence companies effectively vanishing
        # in practice - reviewers don't retype names from a paragraph of flag
        # text). This only changes the DEFAULT (excluded unless opted in);
        # a borderline OCR fragment still won't auto-pollute the mask list.
        entities = []
        skipped_low_confidence = []
        for key, ent in merged.items():
            ent["occurrences"] = (
                _count_occurrences(ent["surface_text"], chunks)
                + image_occurrences_by_key.get(key, 0)
                + alt_text_occurrences_by_key.get(key, 0)
                + sensitive_text_occurrences_by_key.get(key, 0)
            )
            # Surfaced for the reviewer/frontend even though nothing sets it
            # yet - default null reproduces today's [CLIENT_N] behavior
            # exactly; a reviewer can set edits.entity_aliases at review time.
            ent.setdefault("custom_replacement", None)
            # Phase 3: what happens to this entity if the reviewer never
            # touches it - "mask" (Phase 1/2 behavior, unchanged), "flag"
            # (proposed but NOT masked by default), or "mandatory" (Phase
            # 2's CREDENTIAL). INTERNAL_TEAM_MEMBER's default depends on a
            # per-person consent lookup, computed fresh here rather than
            # threaded through every merge site above.
            consent_status = (
                dictionary.get_consent_status(db, ent["surface_text"])
                if ent["entity_type"] == "INTERNAL_TEAM_MEMBER" else None
            )
            ent["default_action"] = entity_actions.resolve_default_action(ent["entity_type"], consent_status)
            entity_threshold = settings.SANITIZATION_CONFIDENCE_THRESHOLDS.get(ent["entity_type"], 0.6)
            if not ent["known"] and ent["confidence"] < entity_threshold:
                skipped_low_confidence.append(ent)
                continue
            entities.append(ent)
        if skipped_low_confidence:
            names = [e["surface_text"] for e in skipped_low_confidence]
            thresholds_used = sorted({settings.SANITIZATION_CONFIDENCE_THRESHOLDS.get(e["entity_type"], 0.6) for e in skipped_low_confidence})
            threshold_desc = f"{thresholds_used[0]:.0%}" if len(thresholds_used) == 1 else "its entity type's"
            flags.append(AgentFlag(
                message=(
                    f"{len(skipped_low_confidence)} low-confidence candidate(s) excluded from the proposal "
                    f"(below {threshold_desc} confidence): "
                    f"{', '.join(names[:10])}{'…' if len(names) > 10 else ''}. "
                    "See 'Excluded candidates' below to include any that matter."
                ),
                severity="info",
            ))

        # Step 5: Sensitive Outcomes (Phase 3) - a document-level read over
        # the ORIGINAL text, separate from every span-level detector above.
        # Must run here, not folded into summarizer.py's pass (which only
        # ever sees the masked text, in apply(), after approval) - a
        # reviewer needs to see this BEFORE deciding whether to approve.
        discusses_negative_outcome = False
        negative_outcome_excerpts: list[str] = []
        negative_outcome_summary = ""
        if settings.SANITIZATION_NEGATIVE_OUTCOME_CHECK_ENABLED and full_text.strip():
            t = time.monotonic()
            outcome_resp = await sensitive_outcome.check_negative_outcome(full_text)
            outcome_parsed = outcome_resp.parsed or {}
            discusses_negative_outcome = bool(outcome_parsed.get("discusses_negative_outcome", False))
            negative_outcome_excerpts = [s for s in (outcome_parsed.get("excerpts") or []) if isinstance(s, str)]
            negative_outcome_summary = outcome_parsed.get("summary", "") or ""
            if discusses_negative_outcome:
                flags.append(AgentFlag(
                    message=(
                        f"This document discusses a sensitive outcome: {negative_outcome_summary or '(see excerpts)'} "
                        "— a reviewer must explicitly resolve this before the run can be approved."
                    ),
                    severity="warning",
                ))
            steps.append(AgentStep(
                order=5, name="sensitive outcome check", tool="bedrock",
                detail=(
                    f"discusses_negative_outcome={discusses_negative_outcome}"
                    f"; {outcome_resp.input_tokens}+{outcome_resp.output_tokens} tok"
                ),
                duration_ms=int((time.monotonic() - t) * 1000),
            ))
        else:
            outcome_resp = None

        # Step 6: file the review. Direct write, not a model-callable tool -
        # this was never actually reachable from the LLM tool-use loop even
        # under the old MCP setup (agent code called it directly), so it's
        # a "protected call" by the same guardrail sanitization/tools.py
        # documents: never put in a TOOLS dict the model can invoke.
        summary = (
            f"Sanitization proposes masking {len(entities)} client entit{'y' if len(entities) == 1 else 'ies'}"
            f" in '{doc.filename}'" + (f", and flags {len(flagged_groups)} image(s) for review" if flagged_groups else "") + "."
        )
        db.add(ReviewItem(run_id=run.id, notes=summary))
        db.commit()
        steps.append(AgentStep(order=6, name="file review", tool=None, detail=summary))

        images_proposal = [
            {
                "group_index": g.group_index,
                "sample_index": g.sample_ref.index,
                # Every occurrence in the cluster, INCLUDING SHA-distinct
                # near-duplicate renditions merged by perceptual dedup - their
                # bytes differ from the sample's, so apply() cannot re-derive
                # this set from the sample image alone.
                "all_indices": g.all_indices,
                "locations": g.locations,
                "occurrence_count": len(g.all_indices),
                "contains_client_identity": g.contains_client_identity,
                "description": g.description,
                "confidence": g.confidence,
                "ocr_text": g.ocr_text,
                "ocr_matched_surface": g.ocr_matched_surface,
                "logo_match_token": db.get(MaskingEntity, g.logo_match_entity_id).mask_token if g.logo_match_entity_id else None,
                "logo_match_distance": g.logo_match_distance,
                "needs_human_judgment": g.needs_human_judgment,
                "phash": g.phash,
                # Phase 2 (Data Samples / Infra & Security).
                "contains_real_data_sample": g.contains_real_data_sample,
                "sensitive_text_matches": [{"surface_text": s, "entity_type": t} for s, t in g.sensitive_text_matches],
                # A confirmed perceptual-hash match to an entity that's
                # ALREADY approved in the masking dictionary is a governance
                # decision that was already made (when that entity was
                # approved) - not something a per-image checkbox should be
                # able to re-open. Observed: the exact same confirmed-match
                # image got excluded three runs in a row despite increasingly
                # explicit description text, because the vision model's own
                # free-text commentary kept arguing the opposite. apply()
                # enforces this regardless of excluded_image_groups. A
                # CREDENTIAL read off this image (Phase 2) gets the exact
                # same non-overridable treatment - a live credential should
                # never be reviewer-optional, image or text.
                "mandatory_redaction": (
                    (
                        g.logo_match_distance is not None
                        and g.logo_match_distance <= MATCH_THRESHOLD
                        and g.logo_match_entity_id is not None
                        and db.get(MaskingEntity, g.logo_match_entity_id).status == "approved"
                    )
                    or any(etype in infra_credential.CREDENTIAL_TYPES for _, etype in g.sensitive_text_matches)
                ),
            }
            for g in image_groups
        ]

        return ReviewProposal(
            summary=summary,
            needs_review=True,
            proposal={
                "document_id": str(document_id), "filename": doc.filename, "total_chunks": len(chunks),
                "entities": entities, "images": images_proposal, "images_skipped": skipped,
                "excluded_entities": skipped_low_confidence,
                # Phase 3 (Sensitive Outcomes) - document-level, see Step 5
                # above. discusses_negative_outcome gates review submission
                # (review/routes.py's submit_review) rather than the mask
                # table's per-entity checkboxes.
                "discusses_negative_outcome": discusses_negative_outcome,
                "negative_outcome_excerpts": negative_outcome_excerpts,
                "negative_outcome_summary": negative_outcome_summary,
            },
            steps=steps,
            flags=flags,
            input_tokens=resp.input_tokens + img_in + (outcome_resp.input_tokens if outcome_resp else 0),
            output_tokens=resp.output_tokens + img_out + (outcome_resp.output_tokens if outcome_resp else 0),
            estimated_cost_usd=resp.estimated_cost_usd + img_cost + (outcome_resp.estimated_cost_usd if outcome_resp else 0.0),
            working_status="detecting",
        )

    async def apply(self, db: Session, run: AgentRun, decision: dict) -> AgentResult:
        steps: list[AgentStep] = []
        flags: list[AgentFlag] = []
        proposal = decision.get("proposal") or (run.output_json or {}).get("proposal") or {}
        edits = decision.get("edits") or {}
        removed = {s.lower() for s in edits.get("removed_surfaces", [])}
        # Phase 3: the opt-IN counterpart to removed_surfaces, for every
        # entity whose default_action is "flag"/"keep" - see
        # _resolve_entity_inclusion.
        included = {s.lower() for s in edits.get("included_surfaces", [])}
        # Phase 3 (Internal Team consent) - see _apply_immediate_consent_grants.
        consent_updates = edits.get("consent_updates") or {}
        removed = _apply_immediate_consent_grants(proposal.get("entities", []), removed, consent_updates)

        document_id = proposal["document_id"]
        doc = db.get(UploadedDocument, uuid.UUID(str(document_id)))
        chunks = extract_chunks(doc.stored_path, doc.content_type, doc.filename)

        # CREDENTIAL is mandatory and non-overridable, the same "reviewer
        # cannot exclude this" contract already enforced for a confirmed
        # own-firm logo match (see the images path below) - a live
        # credential should never be reviewer-optional. Ignoring the removal
        # request here (rather than validating it earlier and rejecting the
        # whole submission) mirrors the existing pattern for entity_merges'
        # unresolvable-canonical-surface case: degrade to the safe behavior
        # and flag it, don't fail the run.
        entities, blocked_credential_removals = _resolve_entity_inclusion(proposal.get("entities", []), removed, included)
        if blocked_credential_removals:
            flags.append(AgentFlag(
                message=(
                    f"{len(blocked_credential_removals)} CREDENTIAL entit"
                    f"{'y' if len(blocked_credential_removals) == 1 else 'ies'} cannot be excluded from masking "
                    f"(mandatory, non-overridable): {', '.join(blocked_credential_removals)}."
                ),
                severity="warning",
            ))

        # Phase 3 (Internal Team consent workflow): persisted globally so
        # every FUTURE run also sees it (same as an alias or a client-
        # account link) - this run's own masking outcome was already
        # decided above, by folding a fresh "granted" into `removed`.
        consent_updated = 0
        for surface, status in consent_updates.items():
            if status not in ("not_required", "pending", "granted"):
                continue
            consent_entity = dictionary.get_or_create(db, surface, "INTERNAL_TEAM_MEMBER", run.id, approved=True)
            dictionary.set_consent_status(db, consent_entity, status)
            consent_updated += 1
        if consent_updated:
            steps.append(AgentStep(order=1, name="internal team consent", tool="masking_dictionary",
                                   detail=f"{consent_updated} consent record(s) updated"))

        # Reviewer-edit deltas as precision/recall signal (Task 3) - see
        # review_deltas.py. Purely additive instrumentation: does not change
        # what apply() actually does with the edits.
        precision_miss_by_type = review_deltas.precision_miss_by_type(proposal.get("entities", []), removed)

        # Reviewer-added entities the agent missed entirely (e.g. a name only
        # visible in an image, or a genuine miss). Merged in before masking so
        # they go through the exact same dictionary + mask + occurrence path
        # as anything the agent proposed itself.
        existing_keys = {dictionary.normalize(e["surface_text"]) for e in entities}
        added_entities = edits.get("added_entities", [])
        new_added, recall_miss_by_type = review_deltas.resolve_added_entities(added_entities, existing_keys)
        for added in new_added:
            surface = added["surface_text"]
            entities.append({
                "surface_text": surface,
                "entity_type": added["entity_type"],
                "confidence": 1.0,
                "known": False,
                "mask_token": None,
                "added_by_reviewer": True,
                "occurrences": _count_occurrences(surface, chunks),
            })
        if added_entities:
            steps.append(AgentStep(order=1, name="reviewer additions", tool=None,
                                   detail=f"{len(added_entities)} entit{'y' if len(added_entities) == 1 else 'ies'} added by reviewer"))
        if recall_miss_by_type or precision_miss_by_type:
            steps.append(AgentStep(
                order=1, name="review deltas", tool=None,
                detail=f"recall misses (model missed, reviewer added): {recall_miss_by_type or '{}'}; "
                       f"precision misses (model over-flagged, reviewer removed): {precision_miss_by_type or '{}'}",
            ))

        # Resolve/allocate global mask tokens; approve on reviewer sign-off.
        # Client-account linkage is a SEPARATE, explicit reviewer choice below -
        # we never again guess "the client" from whichever entity happened to
        # be found first. A document naming its client will usually also name
        # other companies (competitors, portfolio holdings, vendors); those
        # are not "the account" just because they got masked too.
        #
        # Reviewer-chosen aliases (Task C): edits.entity_aliases maps a
        # proposal surface_text to a replacement string the reviewer wants
        # used everywhere INSTEAD of the [CLIENT_N] token, for the entity
        # that surface resolves to - validated (deterministic dictionary
        # collision checks + an LLM "is this a real org" check) before being
        # persisted, since an alias itself can create a NEW leak (aliasing a
        # masked client to another real company's name) rather than hiding
        # the original one. A rejected alias falls back to the ordinary
        # token - it never silently ships unvalidated, and never blocks the
        # run (see the "warning" severity below, not "blocking").
        # Cross-surface entity merging: edits.entity_merges maps a proposal
        # surface_text to ANOTHER surface_text the reviewer has recognized as
        # the SAME real-world entity (e.g. "J&J" -> "Johnson & Johnson") -
        # the detector proposes each distinct string it finds as its OWN
        # entity, with no automatic linking, so two spellings of one client
        # would otherwise get two different [CLIENT_N] tokens (and two
        # different aliases, if Task C's alias were only set on one of them).
        # Merge SOURCES are resolved AFTER every other entity (including the
        # merge TARGET), so the canonical entity always already exists in
        # surface_to_entity by the time a source needs to look it up.
        entity_merges = edits.get("entity_merges") or {}
        merge_source_surfaces = {
            e["surface_text"] for e in entities
            if e["surface_text"] in entity_merges and (entity_merges[e["surface_text"]] or "").strip()
        }
        ordered_entities = (
            [e for e in entities if e["surface_text"] not in merge_source_surfaces]
            + [e for e in entities if e["surface_text"] in merge_source_surfaces]
        )

        entity_aliases = edits.get("entity_aliases") or {}
        alias_flags_raised = 0
        alias_validation_in = alias_validation_out = 0
        alias_validation_cost = 0.0
        surface_to_token: dict[str, str] = {}
        surface_to_entity: dict[str, object] = {}
        t = time.monotonic()
        for ent in ordered_entities:
            surface = ent["surface_text"]

            if surface in merge_source_surfaces:
                canonical_surface = entity_merges[surface].strip()
                canonical_entity = surface_to_entity.get(canonical_surface.lower())
                if canonical_entity is not None:
                    # Link permanently in the global dictionary (same as any
                    # other alias) so future documents also recognize this
                    # surface as the same entity, not just this run.
                    dictionary.add_alias(db, canonical_entity, surface)
                    surface_to_token[surface] = dictionary.resolved_replacement(canonical_entity)
                    surface_to_entity[surface.lower()] = canonical_entity
                    continue
                # Canonical surface wasn't among this run's resolved entities
                # (e.g. a typo, or it was itself removed by the reviewer) -
                # flag it and fall through to ordinary resolution so this
                # surface still gets masked on its own rather than silently
                # vanishing from surface_to_token entirely.
                flags.append(AgentFlag(
                    message=(
                        f"Could not merge '{surface}' into '{canonical_surface}' - the canonical surface wasn't "
                        f"found among this run's resolved entities. '{surface}' was masked as its own separate "
                        "entity instead."
                    ),
                    severity="warning",
                ))

            entity = dictionary.get_or_create(db, surface, ent["entity_type"], run.id, approved=True)
            dictionary.approve(db, entity)

            requested_alias = (entity_aliases.get(surface) or "").strip()
            if requested_alias:
                problems = dictionary.validate_custom_replacement(db, entity, requested_alias)
                if not problems and settings.SANITIZATION_ALIAS_VALIDATION_ENABLED:
                    try:
                        alias_resp = await alias_validate.validate_alias(requested_alias)
                        alias_validation_in += alias_resp.input_tokens
                        alias_validation_out += alias_resp.output_tokens
                        alias_validation_cost += alias_resp.estimated_cost_usd
                        alias_parsed = alias_resp.parsed or {}
                        if alias_parsed.get("is_real_organization"):
                            problems.append(
                                f"'{requested_alias}' appears to itself name a real organization: "
                                f"{alias_parsed.get('reason', '(no reason given)')}"
                            )
                    except Exception as exc:
                        problems.append(f"could not validate against the LLM real-organization check: {exc}")
                if problems:
                    alias_flags_raised += 1
                    flags.append(AgentFlag(
                        message=(
                            f"Alias '{requested_alias}' for {entity.mask_token} was rejected - falling back to the "
                            f"token instead: {'; '.join(problems)}"
                        ),
                        severity="warning",
                    ))
                else:
                    dictionary.set_custom_replacement(db, entity, requested_alias)

            surface_to_token[surface] = dictionary.resolved_replacement(entity)
            surface_to_entity[surface.lower()] = entity
        steps.append(AgentStep(order=2, name="resolve masks", tool="masking_dictionary",
                               detail=f"{len(surface_to_token)} entities → global tokens"
                                      + (f"; {alias_flags_raised} alias(es) rejected" if alias_flags_raised else ""),
                               duration_ms=int((time.monotonic() - t) * 1000)))
        surface_to_entity_id = {s: e.id for s, e in surface_to_entity.items()}

        # Link ONLY the single entity the reviewer explicitly designated as
        # "the client" (if any) to a client account. No selection = no link;
        # masking still happens for every entity regardless.
        client_account_id = None
        client_entity_surface = (edits.get("client_entity_surface") or "").strip().lower()
        if client_entity_surface and client_entity_surface in surface_to_entity:
            matching = next((e for e in entities if e["surface_text"].strip().lower() == client_entity_surface), None)
            if matching:
                account = registry.get_or_create_client_account(db, matching["surface_text"])
                client_account_id = account.id
                dictionary.approve(db, surface_to_entity[client_entity_surface], client_account_id=client_account_id)
        registry.capture_identity(db, run.id, {"entities": [e["surface_text"] for e in entities]}, client_account_id)

        # Reviewer-chosen sanitization style: replace with a traceable mask
        # token (default), a solid black block, or delete the text outright.
        masking_style = resolve_style(edits.get("masking_style"))

        # Deterministic masking.
        t = time.monotonic()
        masked_chunks, occurrences = apply_masks(chunks, surface_to_token, style=masking_style)
        for occ in occurrences:
            db.add(MaskingOccurrence(
                run_id=run.id, entity_id=surface_to_entity_id.get(occ.surface_text.lower()),
                chunk_id=occ.chunk_id, start_offset=occ.start_offset, end_offset=occ.end_offset, surface_text=occ.surface_text,
            ))
        masked_text = "\n\n".join(c["text"] for c in masked_chunks)
        steps.append(AgentStep(order=3, name="apply masks", tool=None,
                               detail=f"{len(occurrences)} occurrences masked across {len(masked_chunks)} chunks",
                               duration_ms=int((time.monotonic() - t) * 1000)))

        # Summarize for Tagging (over masked text).
        t = time.monotonic()
        summ = await summarizer.summarize(masked_text)
        parsed = summ.parsed or {}
        db.add(DocumentMetadata(
            run_id=run.id, sanitized_summary=parsed.get("sanitized_summary"), metadata_json=parsed.get("metadata", {}),
        ))
        steps.append(AgentStep(order=4, name="summarize", tool="bedrock",
                               detail=f"metadata for Tagging; {summ.input_tokens}+{summ.output_tokens} tok",
                               duration_ms=int((time.monotonic() - t) * 1000)))

        # Precision QA (advisory, opt-in): verification above only checks that
        # a FLAGGED surface disappeared - it is structurally blind to whether
        # a masked token should have been flagged at all (the observed
        # "[CLIENT_18] Allocation" from "Capital Allocation" failure). This is
        # the other half: a read over the masked text itself, never mutating
        # anything, never blocking - a reviewer decides what to do with it.
        precision_resp = None
        if settings.SANITIZATION_PRECISION_CHECK_ENABLED and surface_to_token:
            t = time.monotonic()
            precision_resp = await precision_check.check_precision(masked_text, list(surface_to_token.values()))
            precision_flags = (precision_resp.parsed or {}).get("flags", []) if precision_resp else []
            for pf in precision_flags:
                flags.append(AgentFlag(
                    message=(
                        f"Possible over-redaction: {pf.get('mask_token')} in \"{pf.get('surrounding_text')}\" — "
                        f"{pf.get('reason')} (confidence {float(pf.get('confidence', 0)):.0%})."
                    ),
                    severity="advisory",
                ))
            steps.append(AgentStep(
                order=4, name="precision check", tool="bedrock",
                detail=f"{len(precision_flags)} possible over-redaction(s) flagged"
                       + (f"; {precision_resp.input_tokens}+{precision_resp.output_tokens} tok" if precision_resp else ""),
                duration_ms=int((time.monotonic() - t) * 1000),
            ))

        # Mosaic re-identification QA (advisory, opt-in): every detector above
        # reasons about SURFACE STRINGS - none of them can catch a client
        # still being identifiable through co-occurring facts left in the
        # clear (an acquisition, an AUM figure, a partnership date) after the
        # name itself was masked. Adversarial: the model is explicitly told
        # to try to unmask the document, not to judge it charitably.
        reidentify_resp = None
        if settings.SANITIZATION_REIDENTIFY_ENABLED and surface_to_token:
            t = time.monotonic()
            reidentify_resp = await reidentify.reidentify(masked_text, list(surface_to_token.values()))
            guesses = (reidentify_resp.parsed or {}).get("guesses", []) if reidentify_resp else []
            reid_flags = [g for g in guesses if float(g.get("confidence", 0)) >= settings.SANITIZATION_REIDENTIFY_THRESHOLD]
            for g in reid_flags:
                phrases = ", ".join(g.get("leaking_phrases") or [])
                flags.append(AgentFlag(
                    message=(
                        f"Possible re-identification risk: {g.get('mask_token')} may be inferable as "
                        f"'{g.get('candidate_org')}' (confidence {float(g.get('confidence', 0)):.0%}) from: {phrases}."
                    ),
                    severity="advisory",
                ))
            steps.append(AgentStep(
                order=4, name="re-identification check", tool="bedrock",
                detail=f"{len(reid_flags)} of {len(guesses)} token(s) at/above {settings.SANITIZATION_REIDENTIFY_THRESHOLD:.0%} re-identification confidence"
                       + (f"; {reidentify_resp.input_tokens}+{reidentify_resp.output_tokens} tok" if reidentify_resp else ""),
                duration_ms=int((time.monotonic() - t) * 1000),
            ))

        run_id = str(run.id)

        # Resolve which images the reviewer approved for redaction BEFORE
        # rendering: xlsx redacts images in the SAME pass as text (see
        # render.py._render_xlsx) because openpyxl renumbers every image's
        # media partname on save, so a partname resolved only after an xlsx
        # render would not match that already-rendered file - the same
        # silent-failure shape this whole feature exists to close.
        all_image_refs = extract_images(doc.stored_path, doc.content_type, doc.filename)
        by_index = {ref.index: ref for ref in all_image_refs}
        image_groups_proposal = proposal.get("images", [])
        excluded_groups = set(edits.get("excluded_image_groups", []))
        included_groups = set(edits.get("included_image_groups", []))  # reviewer opt-in for non-flagged images
        approved_refs = []
        approved_groups = []
        for g in image_groups_proposal:
            # contains_real_data_sample (Phase 2) is recommended-by-default
            # the same way contains_client_identity already is - this MUST
            # match the frontend's default checkbox state (willRedact() in
            # ReviewDetailPage.jsx), or a data-sample image the reviewer
            # leaves untouched (no explicit include/exclude entry, because
            # nothing was toggled away from its default) would show as
            # checked in the UI but silently not be redacted here.
            recommended = (
                (g.get("contains_client_identity") or g.get("contains_real_data_sample"))
                and g["group_index"] not in excluded_groups
            )
            opted_in = g["group_index"] in included_groups
            # A confirmed logo-hash match to an already-approved entity can't
            # be excluded via the checkbox - see mandatory_redaction above.
            if not (recommended or opted_in or g.get("mandatory_redaction")):
                continue
            approved_groups.append(g)
            # Redact every occurrence in the cluster. all_indices is
            # authoritative: perceptual dedup merges SHA-DISTINCT renditions
            # of the same logo (different compression/resize) into one group,
            # and those renditions are NOT byte-equal to the sample - a
            # byte-equality sweep alone silently leaves them in the rendered
            # file (observed: 2 confident logo matches surviving apply).
            indices = g.get("all_indices")
            if indices:
                approved_refs.extend(by_index[i] for i in indices if i in by_index)
                continue
            # Fallback for proposals filed before all_indices existed:
            # byte-equality with the sample (correct for exact-SHA groups).
            sample_idx = g.get("sample_index")
            sample_bytes = by_index[sample_idx].image_bytes if sample_idx in by_index else None
            for ref in all_image_refs:
                if sample_bytes is not None and ref.image_bytes == sample_bytes:
                    approved_refs.append(ref)

        # Reviewer-chosen aliases (Task C), image side: an approved group's
        # ocr_matched_surface (or the designated "client" surface, for a
        # logo-hash-only match with no OCR text) resolves to the SAME entity
        # object already built above - if THAT entity has a custom
        # replacement, every occurrence in the group gets it as the
        # placeholder's label instead of the default "REDACTED".
        image_labels: dict[int, str] = {}
        for g in approved_groups:
            ocr_matched = (g.get("ocr_matched_surface") or "").strip().lower()
            linked_entity = surface_to_entity.get(ocr_matched) or surface_to_entity.get(client_entity_surface)
            if linked_entity and linked_entity.custom_replacement:
                for idx in (g.get("all_indices") or []):
                    image_labels[idx] = linked_entity.custom_replacement

        # Diagnostic (not cosmetic): makes "did the reviewer actually approve
        # this image, or was it never flagged in the first place" answerable
        # from the Step Timeline instead of requiring a guess after the fact -
        # the previous silence here is exactly what made a real incident
        # (flagged logos surviving to the rendered file with 0 redacted)
        # impossible to root-cause from the run record alone.
        flagged_count = sum(1 for g in image_groups_proposal if g.get("contains_client_identity"))
        steps.append(AgentStep(
            order=5, name="image approval decision", tool=None,
            detail=(
                f"{len(image_groups_proposal)} image group(s) in proposal, {flagged_count} flagged by detection; "
                f"reviewer excluded {sorted(excluded_groups)}, opted in {sorted(included_groups)}; "
                f"{len(approved_groups)} group(s) approved for redaction -> {len(approved_refs)} occurrence(s) resolved"
            ),
        ))

        # Render the sanitized document in the SAME format the user uploaded
        # (masked PDF/DOCX/PPTX/XLSX), and also keep a plain-text copy for the inline viewer.
        rendered_natively = True
        xlsx_images_redacted = 0
        try:
            masked_doc_path, xlsx_images_redacted = render_masked_document(
                run_id, doc.stored_path, doc.content_type, doc.filename, surface_to_token,
                style=masking_style, approved_image_refs=approved_refs, image_labels=image_labels,
            )
        except Exception as exc:
            rendered_natively = False
            masked_doc_path = save_masked_document(run_id, doc.filename, masked_chunks)
            flags.append(AgentFlag(message=f"Could not render masked {doc.filename} in its original format ({exc}); a plain-text copy was saved instead — the downloadable file is NOT the original format.", severity="blocking"))
        save_masked_document(run_id, doc.filename, masked_chunks)  # always keep the .txt for inline view
        steps.append(AgentStep(order=6, name="render sanitized document", tool=None, detail=f"Wrote {masked_doc_path}"))

        # Redact reviewer-approved images for non-xlsx formats (xlsx already
        # redacted them above, in the same pass as text).
        is_xlsx = doc.filename.lower().endswith(".xlsx") or "spreadsheetml" in doc.content_type
        images_redacted = xlsx_images_redacted
        images_unlocated = 0
        if rendered_natively and approved_refs and not is_xlsx:
            images_redacted, images_unlocated = redact_images(
                masked_doc_path, doc.content_type, doc.filename, approved_refs, labels=image_labels,
            )
        if rendered_natively and images_redacted:
            steps.append(AgentStep(order=7, name="redact images", tool=None, detail=f"{images_redacted} image(s) blacked out"))
        if images_unlocated:
            flags.append(AgentFlag(
                message=f"{images_unlocated} approved image redaction(s) could not be located on the rendered page (a rare PDF pattern-fill case) and were NOT redacted — check manually.",
                severity="blocking",
            ))

        # Strip EXIF from EVERY embedded image, not only ones flagged for
        # redaction - a non-logo photo (site visit, whiteboard snapshot) can
        # carry GPS/device metadata that redaction, which only touches
        # flagged images, never reaches. Runs after redaction so an already-
        # redacted image is just our own placeholder PNG (no EXIF, no-op).
        exif_stripped = 0
        if rendered_natively:
            exif_stripped = strip_exif(masked_doc_path, doc.content_type, doc.filename)
            if exif_stripped:
                steps.append(AgentStep(order=7, name="strip image EXIF", tool=None, detail=f"{exif_stripped} image(s) had EXIF/metadata removed"))

        # Scrub image alt-text (descr/title/name on cNvPr/docPr) - redacting
        # a logo's PIXELS above does nothing to its alt-text, a completely
        # separate element; this closed a real leak (21 client names read
        # straight off descr="..." attributes on logos whose pixels were
        # already fully redacted).
        alt_text_scrubbed = 0
        if rendered_natively:
            alt_text_scrubbed = scrub_alt_text(masked_doc_path, doc.content_type, doc.filename, surface_to_token, masking_style)
            if alt_text_scrubbed:
                steps.append(AgentStep(order=7, name="scrub image alt-text", tool=None, detail=f"{alt_text_scrubbed} descr/title/name attribute(s) rewritten"))

        # Scrub document metadata (core/app/custom properties) - a client name
        # can sit in "Company" or a custom property with zero occurrences in
        # any text run, so text-run masking above never touches it.
        metadata_scrubbed = 0
        if rendered_natively:
            metadata_scrubbed = scrub_metadata(masked_doc_path, doc.content_type, doc.filename, surface_to_token, masking_style)
            if metadata_scrubbed:
                steps.append(AgentStep(order=8, name="scrub metadata", tool=None, detail=f"{metadata_scrubbed} propert{'y' if metadata_scrubbed == 1 else 'ies'} rewritten"))

        # Scrub hyperlink targets and comments/track-changes - the two channels
        # that used to be detect-and-block only (a genuine href like
        # https://www.<client>.com permanently blocked a run with no path to
        # clean). Both now self-heal; verification below still re-checks the
        # result and blocks anything these missed.
        hyperlinks_scrubbed = comments_scrubbed = 0
        if rendered_natively:
            hyperlinks_scrubbed = scrub_hyperlinks(masked_doc_path, doc.content_type, doc.filename, surface_to_token, masking_style)
            if hyperlinks_scrubbed:
                steps.append(AgentStep(order=8, name="scrub hyperlink targets", tool=None, detail=f"{hyperlinks_scrubbed} hyperlink target(s) rewritten"))
            comments_scrubbed = scrub_comments(masked_doc_path, doc.content_type, doc.filename, surface_to_token, masking_style)
            if comments_scrubbed:
                steps.append(AgentStep(order=8, name="scrub comments/track-changes", tool=None, detail=f"{comments_scrubbed} comment/tracked-change fragment(s) rewritten"))

        # Multi-channel verification: re-derive each answer from the RENDERED
        # file rather than trusting any earlier computation - a channel this
        # never checks is a channel that can silently leak, which is exactly
        # how the logo bug slipped through when only text was ever checked.
        verified_text = verified_images = verified_metadata = verified_comments = verified_hyperlinks = None
        native_masking_verified = None
        residual_image_groups = []
        if rendered_natively:
            surfaces = list(surface_to_token.keys())
            t = time.monotonic()
            residual_text = find_residual_surfaces(masked_doc_path, doc.content_type, doc.filename, surfaces)
            residual_image_groups, residual_images_skipped, _, _, _ = await find_residual_image_groups(
                masked_doc_path, doc.content_type, doc.filename, db, run_id=run.id
            )
            residual_images = residual_image_messages(residual_image_groups, residual_images_skipped)
            residual_metadata = find_residual_metadata(masked_doc_path, doc.content_type, doc.filename, surfaces)
            residual_comments = find_residual_comments(masked_doc_path, doc.content_type, doc.filename, surfaces)
            residual_hyperlinks = find_residual_hyperlinks(masked_doc_path, doc.content_type, doc.filename, surfaces)

            for channel_name, residual in (
                ("text", residual_text), ("images", residual_images), ("metadata", residual_metadata),
                ("comments", residual_comments), ("hyperlinks", residual_hyperlinks),
            ):
                if residual:
                    flags.append(AgentFlag(
                        message=f"Verification failed ({channel_name}): {len(residual)} item(s) still expose a masked term in the rendered {doc.filename.split('.')[-1].upper()} — {', '.join(residual[:5])}{'…' if len(residual) > 5 else ''}. Do not distribute this file as-is.",
                        severity="blocking",
                    ))

            verified_text = len(residual_text) == 0
            verified_images = len(residual_images) == 0
            verified_metadata = len(residual_metadata) == 0
            verified_comments = len(residual_comments) == 0
            verified_hyperlinks = len(residual_hyperlinks) == 0
            native_masking_verified = all([verified_text, verified_images, verified_metadata, verified_comments, verified_hyperlinks])
            steps.append(AgentStep(
                order=9, name="verify masking (text/images/metadata/comments/hyperlinks)", tool=None,
                detail="clean across all channels" if native_masking_verified else "one or more channels still expose a masked term",
                duration_ms=int((time.monotonic() - t) * 1000),
            ))

            # Structural safety net (item 1c), not a masking-correctness check:
            # does every zip part that LOOKS identity-bearing have SOME
            # scrubber claiming it? This is exactly the question that would
            # have caught ppt/authors.xml sitting completely outside every
            # channel's scope before it ever shipped. Advisory only - a hit
            # here means "a new/unexpected part exists, go look", not "this
            # run leaked" (the five channels above already answer that).
            coverage_warnings = audit_channel_coverage(masked_doc_path, doc.content_type, doc.filename)
            for warning in coverage_warnings:
                flags.append(AgentFlag(message=f"Channel-coverage audit: {warning}", severity="warning"))

        # Revalidation: a second, independent pass over the RENDERED output
        # that catches what the dictionary-based verification just above
        # structurally cannot - it only confirms a KNOWN surface disappeared,
        # never whether an entity was detected in the first place. Automatic
        # on every completed run (not opt-in like precision/reidentify above)
        # - the whole point is to catch what nobody already knew to look for,
        # and gating it behind manual opt-in would defeat that. Two real
        # leaks found this way in production motivated this: "Arvind
        # Fashions" (a total detection miss sitting in plain body text next
        # to a correctly-masked token) and "[CLIENT_25] Turbine Ltd." (a
        # partial-name fragment left next to its own mask token) - see
        # revalidate.py's docstring for the full story.
        revalidation = None
        redetect_resp = None
        if rendered_natively and settings.SANITIZATION_REVALIDATION_ENABLED:
            t = time.monotonic()
            rendered_chunks = extract_chunks(masked_doc_path, doc.content_type, doc.filename)
            rendered_alt_texts = extract_alt_text(masked_doc_path, doc.content_type, doc.filename, include_name=True)
            rendered_full_text = "\n".join(c.text for c in rendered_chunks) + (
                "\n" + "\n".join(rendered_alt_texts) if rendered_alt_texts else ""
            )
            tokens_used = list(surface_to_token.values())

            boundary_hits = revalidate.find_boundary_leaks(rendered_full_text, tokens_used)

            fresh_hits = []
            if tokens_used:
                redetect_resp = await revalidate.fresh_redetect(rendered_full_text)
                fresh_hits = revalidate.parse_fresh_redetect_hits(redetect_resp)

            residuals = boundary_hits + fresh_hits
            score = revalidate.compute_completeness(len(surface_to_token), len(residuals))
            revalidation = {"version": "v1", "score": score, "residuals": residuals}

            if residuals:
                preview = "; ".join(f"{r['leaked_text']} ({r['lens']}, {r['confidence']:.0%})" for r in residuals[:5])
                flags.append(AgentFlag(
                    message=(
                        f"Revalidation found {len(residuals)} possible residual leak(s) not caught by dictionary-based "
                        f"verification (estimated completeness {score}%): {preview}"
                        f"{'…' if len(residuals) > 5 else ''}. Review and approve via the revalidation panel to re-sanitize."
                    ),
                    severity="blocking",
                ))
            steps.append(AgentStep(
                order=10, name="revalidate (fresh-eyes re-detection)", tool="bedrock" if redetect_resp else None,
                detail=(
                    f"{len(boundary_hits)} boundary leak(s), {len(fresh_hits)} fresh-detect leak(s); "
                    f"estimated completeness {score}%"
                    + (f"; {redetect_resp.input_tokens}+{redetect_resp.output_tokens} tok" if redetect_resp else "")
                ),
                duration_ms=int((time.monotonic() - t) * 1000),
            ))

        # Auto-build the logo reference set from this run's approved image
        # redactions - global, reused across future documents, same pattern
        # as text mask tokens (no manual curation - see app.masking.logo_reference).
        #
        # Deliberately NOT falling back to client_entity_surface here (unlike
        # image_labels above): that fallback attributes ANY approved image
        # with no OCR-derivable text to "the designated client", even when
        # the image is an unrelated chart/screenshot with no logo in it at
        # all. Low-stakes for a display label; wrong for a PERSISTENT,
        # CROSS-DOCUMENT phash signal - a bad attribution here doesn't just
        # look wrong once, it makes future documents' unrelated images start
        # false-positive matching against this client's "logo". Observed
        # real consequence: one entity accumulated 35+ phash rows (all
        # distinct hashes, so not simple dupes - most were miscellaneous
        # approved images from repeated re-runs of the same deck, not logos)
        # after only ~7 runs, which also broke the admin panel's Logos
        # column layout. Only two attributions are trustworthy enough to
        # persist globally: this image's own OCR text names a real entity,
        # or it already perceptually matched a KNOWN prior logo reference
        # for a specific entity (logo_match_token) - both are evidence about
        # THIS image, not a guess based on who else is in the document.
        for g in approved_groups:
            phash = g.get("phash")
            if not phash:
                continue
            ocr_matched = (g.get("ocr_matched_surface") or "").strip().lower()
            logo_match_entity = None
            if g.get("logo_match_token"):
                logo_match_entity = db.query(MaskingEntity).filter(MaskingEntity.mask_token == g["logo_match_token"]).first()
            linked_entity = surface_to_entity.get(ocr_matched) or logo_match_entity
            if linked_entity:
                sample_ref = by_index.get(g.get("sample_index"))
                store_reference(
                    db, linked_entity.id, phash, run.id,
                    image_bytes=sample_ref.image_bytes if sample_ref else None,
                )

        output = {
            "document_id": document_id,
            "filename": doc.filename,
            "masked_document_path": masked_doc_path,
            "masking_style": masking_style,
            "native_masking_verified": native_masking_verified,
            "verified_text": verified_text,
            "verified_images": verified_images,
            "verified_metadata": verified_metadata,
            "verified_comments": verified_comments,
            "verified_hyperlinks": verified_hyperlinks,
            # Structured leftovers from the image verify scan (locations +
            # extraction indices INTO THE MASKED FILE) - lets a remediation
            # pass redact exactly these images later with zero new vision
            # scans. Empty when the images channel verified clean.
            "residual_image_groups": [
                {"locations": g.locations, "all_indices": g.all_indices, "phash": g.phash}
                for g in residual_image_groups
            ],
            "entities_masked": [{"mask_token": t_, "entity_type": next((e["entity_type"] for e in entities if e["surface_text"] == s), None)} for s, t_ in surface_to_token.items()],
            # Reviewer-edit deltas, tagged by entity_type - the cheapest real
            # precision/recall signal this system has, sitting unused before
            # Task 3. Queryable across runs (JSONB) to inform Task 4's
            # per-entity-type threshold tuning instead of guessing.
            "review_deltas": {
                "recall_miss_by_type": recall_miss_by_type,
                "precision_miss_by_type": precision_miss_by_type,
            },
            "occurrence_count": len(occurrences),
            "images_redacted": images_redacted,
            "images_exif_stripped": exif_stripped,
            "revalidation": revalidation,
            "sanitized_summary": parsed.get("sanitized_summary"),
            "metadata": parsed.get("metadata", {}),
            "masked_chunks": masked_chunks,
        }
        output_file = save_run_output(self.agent_id, run_id, {"run_id": run_id, "generated_at": datetime.utcnow().isoformat(), **output})

        total_in = summ.input_tokens
        total_out = summ.output_tokens
        total_cost = summ.estimated_cost_usd
        if precision_resp is not None:
            total_in += precision_resp.input_tokens
            total_out += precision_resp.output_tokens
            total_cost += precision_resp.estimated_cost_usd
        if reidentify_resp is not None:
            total_in += reidentify_resp.input_tokens
            total_out += reidentify_resp.output_tokens
            total_cost += reidentify_resp.estimated_cost_usd
        if redetect_resp is not None:
            total_in += redetect_resp.input_tokens
            total_out += redetect_resp.output_tokens
            total_cost += redetect_resp.estimated_cost_usd
        total_in += alias_validation_in
        total_out += alias_validation_out
        total_cost += alias_validation_cost
        return AgentResult(
            agent_id=self.agent_id,
            output={k: v for k, v in output.items() if k != "masked_chunks"} | {"masked_preview": masked_text[:1500]},
            confidence=min([e["confidence"] for e in entities], default=1.0),
            flags=flags,
            steps=steps,
            input_tokens=total_in,
            output_tokens=total_out,
            estimated_cost_usd=total_cost,
            output_file_path=output_file,
        )
