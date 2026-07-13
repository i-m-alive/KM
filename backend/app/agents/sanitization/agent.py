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
from app.agents.sanitization import detector, summarizer
from app.agents.sanitization.apply_masks import apply_masks
from app.agents.sanitization.image_scan import (
    MAX_IMAGES_SCANNED,
    find_residual_image_groups,
    residual_image_messages,
    scan_document_images,
)
from app.agents.sanitization.ner_prepass import extract_candidates, presidio_available
from app.config import get_settings
from app.documents.comment_scan import find_residual_comments
from app.documents.comment_scrub import scrub_comments
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
from app.masking.style import resolve_style
from app.mcp_client import TOOL_CREATE_REVIEW, call_tool_text, mcp_session
from app.models import AgentRun, DocumentMetadata, MaskingEntity, MaskingOccurrence, UploadedDocument
from app.storage.local_store import save_masked_document, save_run_output

settings = get_settings()


def _count_occurrences(surface: str, chunks) -> int:
    pat = re.compile(re.escape(surface), re.IGNORECASE)
    return sum(len(pat.findall(c.text)) for c in chunks)


class SanitizationAgent(BackgroundAgent):
    agent_id = "sanitization"
    display_name = "Sanitization"
    description = (
        "Removes client-identifying information from a document using a global masking dictionary, "
        "then files the proposed masks for human review before applying them."
    )
    tools = ["bedrock", "naviknow-mcp", "presidio"]
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
        steps.append(AgentStep(order=1, name="pre-pass", tool="presidio" if presidio_available() else "regex",
                               detail=f"{len(chunks)} chunks; {len(candidates)} distinct candidate strings",
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
        full_text = "\n".join(c.text for c in chunks)
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

        # Step 3: LLM Detector via MCP tool-use loop.
        t = time.monotonic()
        resp = await detector.detect_entities(str(document_id), len(chunks), candidates)
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
        image_groups, img_in, img_out, img_cost, skipped = await scan_document_images(doc.stored_path, doc.content_type, doc.filename, db)
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
        for g in image_groups:
            if not (g.contains_client_identity or g.needs_human_judgment):
                continue
            for s in g.ocr_text:
                s = s.strip()
                if len(s) < 2 or s.isdigit() or is_own_firm(s) or dictionary.is_skipped(db, s):
                    continue
                key = dictionary.normalize(s)
                if key in merged:
                    continue
                # A logo-OCR'd fragment shorter than MIN_OCR_ENTITY_LENGTH is
                # too collision-prone with ordinary words/acronyms (e.g. "RIA",
                # "sure") to auto-trust at the image group's full confidence -
                # still surfaced for the reviewer, just capped below the
                # low-confidence bar so it can't be silently pre-approved.
                confidence = g.confidence
                if len(s) < settings.MIN_OCR_ENTITY_LENGTH:
                    confidence = min(confidence, settings.SANITIZATION_CONFIDENCE_THRESHOLD - 0.01)
                merged[key] = {
                    "surface_text": s,
                    "entity_type": "CLIENT_NAME",
                    "confidence": confidence,
                    "known": False,
                    "mask_token": None,
                    "source": "image_ocr",
                }

        # Below-threshold candidates are excluded from the proposal entirely
        # rather than surfaced as a per-term warning - without this, the same
        # borderline OCR fragments (short acronyms, common-word collisions)
        # resurface on every single run of the same document forever. A
        # reviewer can still add one back manually via "add entity" if it
        # genuinely matters; this only changes the default.
        entities = []
        skipped_low_confidence = []
        for ent in merged.values():
            ent["occurrences"] = _count_occurrences(ent["surface_text"], chunks)
            if not ent["known"] and ent["confidence"] < settings.SANITIZATION_CONFIDENCE_THRESHOLD:
                skipped_low_confidence.append(ent["surface_text"])
                continue
            entities.append(ent)
        if skipped_low_confidence:
            flags.append(AgentFlag(
                message=(
                    f"{len(skipped_low_confidence)} low-confidence candidate(s) excluded from the proposal "
                    f"(below {settings.SANITIZATION_CONFIDENCE_THRESHOLD:.0%} confidence): "
                    f"{', '.join(skipped_low_confidence[:10])}{'…' if len(skipped_low_confidence) > 10 else ''}. "
                    "Add manually via 'add entity' if any of these should be masked."
                ),
                severity="info",
            ))

        # Step 5: file the review via the MCP review_create_task tool.
        summary = (
            f"Sanitization proposes masking {len(entities)} client entit{'y' if len(entities) == 1 else 'ies'}"
            f" in '{doc.filename}'" + (f", and flags {len(flagged_groups)} image(s) for review" if flagged_groups else "") + "."
        )
        async with mcp_session() as session:
            await call_tool_text(session, TOOL_CREATE_REVIEW, {"run_id": str(run.id), "summary": summary})
        steps.append(AgentStep(order=5, name="file review", tool="naviknow-mcp", detail=summary))

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
                # A confirmed perceptual-hash match to an entity that's
                # ALREADY approved in the masking dictionary is a governance
                # decision that was already made (when that entity was
                # approved) - not something a per-image checkbox should be
                # able to re-open. Observed: the exact same confirmed-match
                # image got excluded three runs in a row despite increasingly
                # explicit description text, because the vision model's own
                # free-text commentary kept arguing the opposite. apply()
                # enforces this regardless of excluded_image_groups.
                "mandatory_redaction": (
                    g.logo_match_distance is not None
                    and g.logo_match_distance <= MATCH_THRESHOLD
                    and g.logo_match_entity_id is not None
                    and db.get(MaskingEntity, g.logo_match_entity_id).status == "approved"
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
            },
            steps=steps,
            flags=flags,
            input_tokens=resp.input_tokens + img_in,
            output_tokens=resp.output_tokens + img_out,
            estimated_cost_usd=resp.estimated_cost_usd + img_cost,
            working_status="detecting",
        )

    async def apply(self, db: Session, run: AgentRun, decision: dict) -> AgentResult:
        steps: list[AgentStep] = []
        flags: list[AgentFlag] = []
        proposal = decision.get("proposal") or (run.output_json or {}).get("proposal") or {}
        edits = decision.get("edits") or {}
        removed = {s.lower() for s in edits.get("removed_surfaces", [])}

        document_id = proposal["document_id"]
        doc = db.get(UploadedDocument, uuid.UUID(str(document_id)))
        chunks = extract_chunks(doc.stored_path, doc.content_type, doc.filename)

        entities = [e for e in proposal.get("entities", []) if e["surface_text"].lower() not in removed]

        # Reviewer-added entities the agent missed entirely (e.g. a name only
        # visible in an image, or a genuine miss). Merged in before masking so
        # they go through the exact same dictionary + mask + occurrence path
        # as anything the agent proposed itself.
        existing_keys = {dictionary.normalize(e["surface_text"]) for e in entities}
        added_entities = edits.get("added_entities", [])
        for added in added_entities:
            surface = (added.get("surface_text") or "").strip()
            if not surface:
                continue
            key = dictionary.normalize(surface)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            entities.append({
                "surface_text": surface,
                "entity_type": added.get("entity_type") or "CLIENT_NAME",
                "confidence": 1.0,
                "known": False,
                "mask_token": None,
                "added_by_reviewer": True,
                "occurrences": _count_occurrences(surface, chunks),
            })
        if added_entities:
            steps.append(AgentStep(order=1, name="reviewer additions", tool=None,
                                   detail=f"{len(added_entities)} entit{'y' if len(added_entities) == 1 else 'ies'} added by reviewer"))

        # Resolve/allocate global mask tokens; approve on reviewer sign-off.
        # Client-account linkage is a SEPARATE, explicit reviewer choice below -
        # we never again guess "the client" from whichever entity happened to
        # be found first. A document naming its client will usually also name
        # other companies (competitors, portfolio holdings, vendors); those
        # are not "the account" just because they got masked too.
        surface_to_token: dict[str, str] = {}
        surface_to_entity: dict[str, object] = {}
        t = time.monotonic()
        for ent in entities:
            surface = ent["surface_text"]
            entity = dictionary.get_or_create(db, surface, ent["entity_type"], run.id, approved=True)
            dictionary.approve(db, entity)
            surface_to_token[surface] = entity.mask_token
            surface_to_entity[surface.lower()] = entity
        steps.append(AgentStep(order=2, name="resolve masks", tool="masking_dictionary",
                               detail=f"{len(surface_to_token)} entities → global tokens", duration_ms=int((time.monotonic() - t) * 1000)))
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
            recommended = g.get("contains_client_identity") and g["group_index"] not in excluded_groups
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
                style=masking_style, approved_image_refs=approved_refs,
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
            images_redacted, images_unlocated = redact_images(masked_doc_path, doc.content_type, doc.filename, approved_refs)
        if rendered_natively and images_redacted:
            steps.append(AgentStep(order=7, name="redact images", tool=None, detail=f"{images_redacted} image(s) blacked out"))
        if images_unlocated:
            flags.append(AgentFlag(
                message=f"{images_unlocated} approved image redaction(s) could not be located on the rendered page (a rare PDF pattern-fill case) and were NOT redacted — check manually.",
                severity="blocking",
            ))

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
                masked_doc_path, doc.content_type, doc.filename, db
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

        # Auto-build the logo reference set from this run's approved image
        # redactions - global, reused across future documents, same pattern
        # as text mask tokens (no manual curation - see app.masking.logo_reference).
        for g in approved_groups:
            phash = g.get("phash")
            if not phash:
                continue
            ocr_matched = (g.get("ocr_matched_surface") or "").strip().lower()
            linked_entity = surface_to_entity.get(ocr_matched) or surface_to_entity.get(client_entity_surface)
            if linked_entity:
                store_reference(db, linked_entity.id, phash, run.id)

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
            "occurrence_count": len(occurrences),
            "images_redacted": images_redacted,
            "sanitized_summary": parsed.get("sanitized_summary"),
            "metadata": parsed.get("metadata", {}),
            "masked_chunks": masked_chunks,
        }
        output_file = save_run_output(self.agent_id, run_id, {"run_id": run_id, "generated_at": datetime.utcnow().isoformat(), **output})

        total_in = summ.input_tokens
        total_out = summ.output_tokens
        return AgentResult(
            agent_id=self.agent_id,
            output={k: v for k, v in output.items() if k != "masked_chunks"} | {"masked_preview": masked_text[:1500]},
            confidence=min([e["confidence"] for e in entities], default=1.0),
            flags=flags,
            steps=steps,
            input_tokens=total_in,
            output_tokens=total_out,
            estimated_cost_usd=summ.estimated_cost_usd,
            output_file_path=output_file,
        )
