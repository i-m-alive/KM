"""Vision-verdict memoization, scoped to a single run (Task 5).

detect() runs in the worker process; apply()'s post-render verify runs in the
API server process (triggered synchronously from review/routes.py). These are
genuinely different OS processes, so an in-memory cache can't make the same
image content get the same verdict at both points - only a persisted, DB-
backed cache can. Scoped per run_id (not global) so verification still
independently re-derives its answer for every new run; only the SAME run
asking about the SAME image bytes twice is short-circuited.

Deliberately caches ONLY the raw model response (contains_client_identity,
description, confidence, ocr_text, contains_real_data_sample) - never the
deterministic post-processing (OCR-match, own-firm exclusion, logo-hash
override, sensitive-text regex match) image_scan.py builds on top of it,
which must always re-run fresh against current dictionary/logo/regex state.
"""

import uuid

from sqlalchemy.orm import Session

from app.models import VisionVerdictCache


def load_cached_verdict(db: Session, run_id: uuid.UUID | None, content_key: str) -> dict | None:
    """The cached raw vision response for this run + content, or None on a
    cache miss (including when run_id is None - callers with no run context,
    e.g. the offline regression suite, always miss)."""
    if run_id is None:
        return None
    row = (
        db.query(VisionVerdictCache)
        .filter(VisionVerdictCache.run_id == run_id, VisionVerdictCache.content_key == content_key)
        .first()
    )
    if row is None:
        return None
    return {
        "contains_client_identity": row.contains_client_identity,
        "description": row.description,
        "confidence": row.confidence,
        "ocr_text": row.ocr_text,
        "contains_real_data_sample": row.contains_real_data_sample,
    }


def store_verdict(db: Session, run_id: uuid.UUID | None, content_key: str, parsed: dict) -> None:
    if run_id is None:
        return
    db.add(VisionVerdictCache(
        run_id=run_id,
        content_key=content_key,
        contains_client_identity=bool(parsed.get("contains_client_identity", False)),
        description=parsed.get("description", "") or "",
        confidence=float(parsed.get("confidence", 0.0)),
        ocr_text=[s for s in (parsed.get("ocr_text") or []) if isinstance(s, str)],
        contains_real_data_sample=bool(parsed.get("contains_real_data_sample", False)),
    ))
    db.flush()
