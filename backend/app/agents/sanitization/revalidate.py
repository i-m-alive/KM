"""Post-sanitization revalidation agent — a second, independent pass over the
RENDERED masked output that looks for residual client-identifying leaks the
dictionary-based verification in verify.py structurally cannot see, because
that check only confirms that a KNOWN, approved surface disappeared. It has
no way to notice an entity that was never in the dictionary at all.

Two real production leaks motivated this (found by directly inspecting a
completed run's actual output file):
  - "Arvind Fashions" surviving in plain body text, in the same sentence as a
    correctly-masked [CLIENT_15] token a few words earlier - the detector
    never proposed it, and the reviewer didn't add it by hand either. Nothing
    downstream of detection had a chance to catch this; it's a pure recall
    miss, not an application bug.
  - "[CLIENT_25] Turbine Ltd." surviving in an image's alt-text - the
    detector caught the first word of the org's name but not the legal-
    suffix tail, so a fragment of the real name sat directly next to its own
    mask token.

Two lenses, cheapest first:
  - find_boundary_leaks(): deterministic, zero-cost regex sweep for a legal-
    entity-suffix word sitting immediately after a mask token - catches the
    "Turbine Ltd." class of partial-name leak with no LLM call.
  - fresh_redetect(): one adversarial Bedrock pass, re-reading the masked
    text as if for the first time - not reusing the exact detector prompt,
    since the same model reasoning about the same text the same way is
    likely to miss the same things twice. Catches the "Arvind Fashions"
    class of total miss anywhere in the document.

Findings are surfaced data, never auto-applied: nothing in this module calls
the masking dictionary or touches a file. A human approves which residuals
are real; reapply_with_additional_entities() (called only from the API
layer, never the model) is what actually turns an approved residual into a
new dictionary entry and re-masks.
"""

import os
import re

from sqlalchemy.orm import Session

from app.agents.sanitization.apply_masks import apply_masks
from app.documents.alttext_scan import extract_alt_text
from app.documents.alttext_scrub import scrub_alt_text
from app.documents.comment_scan import find_residual_comments
from app.documents.comment_scrub import scrub_comments
from app.documents.extract import extract_chunks
from app.documents.hyperlink_scan import find_residual_hyperlinks
from app.documents.hyperlink_scrub import scrub_hyperlinks
from app.documents.metadata_scan import find_residual_metadata
from app.documents.metadata_scrub import scrub_metadata
from app.documents.render import render_masked_document
from app.documents.verify import find_residual_surfaces
from app.llm import bedrock_client
from app.masking import dictionary
from app.masking.dictionary import is_own_firm
from app.models import AgentRun, MaskingEntity, MaskingOccurrence, RunFlag, UploadedDocument


class RevalidationError(ValueError):
    """A precondition failed - surfaced to the API caller as a 400/409."""


# ---------- Lens 1: deterministic boundary heuristic ----------

_LEGAL_SUFFIXES = (
    "Ltd", "Limited", "Inc", "LLC", "LLP", "Pvt", "Corp", "Corporation", "GmbH",
    "plc", "PLC", "Holdings", "Bank", "Insurance", "Capital", "Partners",
)
_SUFFIX_ALT = "|".join(re.escape(s) for s in _LEGAL_SUFFIXES)
_WORD = r"[A-Z][A-Za-z&]*"


def find_boundary_leaks(text: str, mask_tokens: list[str]) -> list[dict]:
    """A mask token immediately followed by 0-3 capitalized words and then a
    legal-entity suffix (e.g. "[CLIENT_25] Turbine Ltd.") means only PART of
    the original name was masked - the suffix, and any words between the
    token and it, are a fragment of the real name still in the clear. No
    Bedrock call, so no false-negative risk for this specific shape of bug;
    real false-positive risk exists (a generic "the [CLIENT_5] Group"
    reference), which is exactly why every hit here is a candidate for
    review, never an auto-fix."""
    if not mask_tokens or not text:
        return []
    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for token in sorted(set(mask_tokens), key=len, reverse=True):
        # Trailing (?!\w), not \b: \b right after an optional trailing "."
        # backtracks the "." away whenever it's followed by a non-word char
        # (a space, end of string) - "." and " " are both non-word, so \b
        # sees no boundary there and drops the period from the match. (?!\w)
        # has no such ambiguity: it succeeds whether or not "." was consumed,
        # so the (greedy) \.? keeps it, e.g. "Turbine Ltd." not "Turbine Ltd".
        pattern = re.compile(
            rf"{re.escape(token)}\s*((?:{_WORD}\s+){{0,3}}(?:{_SUFFIX_ALT})\.?)(?!\w)"
        )
        for m in pattern.finditer(text):
            leaked = m.group(1).strip()
            key = (token, leaked.lower())
            if key in seen:
                continue
            seen.add(key)
            start, end = max(0, m.start() - 30), min(len(text), m.end() + 30)
            hits.append({
                "lens": "boundary",
                "mask_token": token,
                "leaked_text": leaked,
                "entity_type": "CLIENT_NAME",
                "context": text[start:end].replace("\n", " ").strip(),
                "confidence": 0.75,
            })
    return hits


# ---------- Lens 2: fresh, adversarial re-detection ----------

FRESH_REDETECT_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "surface_text": {"type": "string"},
                    "entity_type": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["surface_text", "entity_type", "confidence"],
            },
        }
    },
    "required": ["entities"],
}

_FRESH_REDETECT_SYSTEM_PROMPT = (
    "You are doing a SECOND, INDEPENDENT pass over an already-sanitized document. Sensitive spans have "
    "already been replaced with mask tokens like [CLIENT_18]. Your job is to find any REMAINING "
    "organization name, person name, or other client-identifying detail still present in plain text - "
    "something the first pass may have missed entirely. Be adversarial: assume the first pass was "
    "imperfect, not that the document is already clean. Do not flag the mask tokens themselves, generic "
    "third-party platform/product names used only as examples (e.g. common marketplaces), or the "
    "delivery firm's own name. For each real finding, give the exact surface text as it appears, an "
    "entity_type (CLIENT_NAME, CLIENT_PERSON, CLIENT_LOCATION, CLIENT_EMAIL_DOMAIN, CLIENT_SYSTEM_NAME, "
    "or CLIENT_CONTRACT_ID), and a confidence 0-1. If nothing remains, return an empty entities list "
    "rather than a low-confidence guess."
)


async def fresh_redetect(masked_text: str) -> bedrock_client.BedrockResponse | None:
    """Returns None (no call made) if there's no text to check."""
    if not masked_text or not masked_text.strip():
        return None
    excerpt = masked_text[:24000]
    return await bedrock_client.converse(
        system_prompt=_FRESH_REDETECT_SYSTEM_PROMPT,
        user_message=f"Masked document:\n\n{excerpt}",
        response_schema=FRESH_REDETECT_SCHEMA,
    )


_TOKEN_LITERAL_RE = re.compile(r"^\[[A-Z_]+_\d+\]$")


def parse_fresh_redetect_hits(resp: bedrock_client.BedrockResponse | None) -> list[dict]:
    """Turn a fresh_redetect response into residual dicts, filtering out
    anything that's obviously not a real finding (a bare mask token echoed
    back, the delivery firm's own name, an empty string)."""
    hits: list[dict] = []
    entities = (resp.parsed or {}).get("entities", []) if resp else []
    for e in entities:
        surface = (e.get("surface_text") or "").strip()
        if not surface or _TOKEN_LITERAL_RE.match(surface) or is_own_firm(surface):
            continue
        hits.append({
            "lens": "fresh_redetect",
            "mask_token": None,
            "leaked_text": surface,
            "entity_type": e.get("entity_type") or "CLIENT_NAME",
            "context": "",
            "confidence": float(e.get("confidence", 0.5)),
        })
    return hits


# ---------- scoring ----------

def compute_completeness(masked_count: int, residual_count: int) -> float:
    """Estimated % of client-identifying entities actually removed: masked /
    (masked + residual). This is an ESTIMATE bounded by this module's own
    detection power, not ground truth - if the lenses above also miss
    something, the score overstates cleanliness. Always surface the residual
    list alongside the score, never the number alone: a single glaring leak
    in a 96%-scored document is still a real problem."""
    total = masked_count + residual_count
    if total == 0:
        return 100.0
    return round(masked_count / total * 100, 1)


# ---------- fix loop: approve residuals, re-mask the rendered file in place ----------

def _surface_to_token_for_run(db: Session, run: AgentRun) -> dict[str, str]:
    """Rebuild the exact mask map this run applied, from its persisted
    occurrences - output_json deliberately carries no client surfaces (same
    reasoning as remediate.py's identically-named helper)."""
    mapping: dict[str, str] = {}
    occurrences = db.query(MaskingOccurrence).filter(MaskingOccurrence.run_id == run.id).all()
    for occ in occurrences:
        if occ.entity_id is None or occ.surface_text in mapping:
            continue
        entity = db.get(MaskingEntity, occ.entity_id)
        if entity is not None:
            mapping[occ.surface_text] = entity.mask_token
    return mapping


def reapply_with_additional_entities(db: Session, run: AgentRun, approved_residuals: list[dict]) -> dict:
    """Reviewer approved one or more revalidation residuals as real leaks:
    turn each into a new global dictionary entry, then re-mask the ALREADY-
    RENDERED file in place - never re-render from the original source
    document, since that file already has correct image redactions baked in
    that this function has no way to reconstruct if it started over. This
    reuses remediate.py's proven technique: point the same renderer at the
    masked file itself. Existing mask tokens are already substituted there,
    so they no-op; only the newly-approved surfaces' literal text (which the
    original render never touched) gets found and masked this time."""
    if run.agent_id != "sanitization":
        raise RevalidationError("Only Sanitization runs can be revalidated")
    output = run.output_json if isinstance(run.output_json, dict) else {}
    masked_path = output.get("masked_document_path")
    if not masked_path or not os.path.exists(masked_path):
        raise RevalidationError("This run's rendered masked file no longer exists on disk")
    doc = db.get(UploadedDocument, output.get("document_id"))
    if doc is None:
        raise RevalidationError("The run's source document record no longer exists")
    content_type, filename = doc.content_type, doc.filename
    masking_style = output.get("masking_style", "token")

    surface_to_token = _surface_to_token_for_run(db, run)

    new_entries: dict[str, str] = {}
    new_entity_by_surface: dict[str, MaskingEntity] = {}
    for item in approved_residuals:
        surface = (item.get("leaked_text") or "").strip()
        if not surface or surface in surface_to_token or surface in new_entries:
            continue
        entity_type = item.get("entity_type") or "CLIENT_NAME"
        entity = dictionary.get_or_create(db, surface, entity_type, run.id, approved=True)
        dictionary.approve(db, entity)
        new_entries[surface] = dictionary.resolved_replacement(entity)
        new_entity_by_surface[surface] = entity
    db.flush()

    if not new_entries:
        return {"applied": 0, "native_masking_verified": output.get("native_masking_verified")}

    combined = {**surface_to_token, **new_entries}

    # Occurrence bookkeeping for the NEW surfaces only, against the masked
    # file's chunks BEFORE this call mutates it - same split (extract once,
    # use for both occurrence-counting and the independent native render) as
    # agent.py's real apply().
    chunks_before = extract_chunks(masked_path, content_type, filename)
    _, fresh_occurrences = apply_masks(chunks_before, new_entries, style=masking_style)
    for occ in fresh_occurrences:
        entity = new_entity_by_surface.get(occ.surface_text)
        db.add(MaskingOccurrence(
            run_id=run.id, entity_id=entity.id if entity else None,
            chunk_id=occ.chunk_id, start_offset=occ.start_offset, end_offset=occ.end_offset,
            surface_text=occ.surface_text,
        ))

    tmp_dst, _ = render_masked_document(
        f"{run.id}-revalidate", masked_path, content_type, filename, combined, style=masking_style,
    )
    os.replace(tmp_dst, masked_path)

    scrub_alt_text(masked_path, content_type, filename, combined, masking_style)
    scrub_metadata(masked_path, content_type, filename, combined, masking_style)
    scrub_hyperlinks(masked_path, content_type, filename, combined, masking_style)
    scrub_comments(masked_path, content_type, filename, combined, masking_style)

    surfaces = list(combined.keys())
    residual_text = find_residual_surfaces(masked_path, content_type, filename, surfaces)
    residual_metadata = find_residual_metadata(masked_path, content_type, filename, surfaces)
    residual_comments = find_residual_comments(masked_path, content_type, filename, surfaces)
    residual_hyperlinks = find_residual_hyperlinks(masked_path, content_type, filename, surfaces)

    # One bounded extra pass of the free deterministic lens against the
    # enlarged token set - not a further LLM call and not a further loop
    # beyond this single re-apply, per the capped fix-loop design.
    rendered_chunks = extract_chunks(masked_path, content_type, filename)
    rendered_alt = extract_alt_text(masked_path, content_type, filename, include_name=True)
    rendered_text = "\n".join(c.text for c in rendered_chunks) + ("\n" + "\n".join(rendered_alt) if rendered_alt else "")
    boundary_residuals = find_boundary_leaks(rendered_text, list(combined.values()))

    verified_text = len(residual_text) == 0
    verified_metadata = len(residual_metadata) == 0
    verified_comments = len(residual_comments) == 0
    verified_hyperlinks = len(residual_hyperlinks) == 0
    verified_images = output.get("verified_images")  # untouched by this pass
    native_masking_verified = all([verified_text, verified_images, verified_metadata, verified_comments, verified_hyperlinks])

    score = compute_completeness(len(combined), len(boundary_residuals))

    run.output_json = {
        **output,
        "verified_text": verified_text,
        "verified_metadata": verified_metadata,
        "verified_comments": verified_comments,
        "verified_hyperlinks": verified_hyperlinks,
        "native_masking_verified": native_masking_verified,
        "entities_masked": (output.get("entities_masked") or []) + [
            {"mask_token": token, "entity_type": new_entity_by_surface[s].entity_type}
            for s, token in new_entries.items()
        ],
        "occurrence_count": int(output.get("occurrence_count") or 0) + len(fresh_occurrences),
        "revalidation": {
            "version": "v1",
            "score": score,
            "residuals": boundary_residuals,
            "applied_count": len(new_entries),
        },
    }

    # Drop the superseded revalidation flag and any now-resolved channel
    # flags, then add fresh ones for whatever still doesn't verify clean -
    # same "don't keep telling the user it's unsafe after it verified clean"
    # discipline as remediate.py's _remove_resolved_flags.
    for flag in list(run.flags):
        if flag.severity == "blocking" and (
            flag.message.startswith("Revalidation found")
            or any(f"Verification failed ({ch})" in flag.message for ch in ("text", "metadata", "comments", "hyperlinks"))
        ):
            db.delete(flag)

    for ch, residual in (
        ("text", residual_text), ("metadata", residual_metadata),
        ("comments", residual_comments), ("hyperlinks", residual_hyperlinks),
    ):
        if residual:
            db.add(RunFlag(
                run_id=run.id,
                message=(
                    f"Verification failed ({ch}) after revalidation fix: {len(residual)} item(s) - "
                    f"{', '.join(residual[:5])}. Do not distribute this file as-is."
                ),
                severity="blocking",
            ))
    if boundary_residuals:
        preview = "; ".join(f"{r['leaked_text']} ({r['confidence']:.0%})" for r in boundary_residuals[:5])
        db.add(RunFlag(
            run_id=run.id,
            message=(
                f"Revalidation found {len(boundary_residuals)} more possible residual leak(s) "
                f"(estimated completeness {score}%): {preview}. Review and approve to re-sanitize again."
            ),
            severity="blocking",
        ))

    run.status = "completed" if (native_masking_verified and not boundary_residuals) else "completed_with_issues"
    db.commit()

    # Second path to "completed" alongside apply()->_finalize_completed and
    # remediate_run - must trigger the same auto-chain hook (see those two
    # for why: a document fixed here should still hand off to Tagging).
    from app.runs.background import _maybe_auto_chain_to_tagging

    _maybe_auto_chain_to_tagging(db, run)

    return {
        "applied": len(new_entries),
        "native_masking_verified": native_masking_verified,
        "revalidation_score": score,
        "remaining_residuals": len(boundary_residuals),
    }
