"""Tag vocabulary + validation — PROTECTED (agent code only). tags.validate is
the deterministic hard gate that makes an out-of-vocabulary tag impossible to
apply; unknown terms become pending_approval proposals for governance."""

import uuid

from sqlalchemy.orm import Session

from app.models import RunTag, TagVocabulary

CATEGORIES = ["domain", "use_case", "technology", "geography", "engagement_type"]


def approved_vocabulary(db: Session) -> dict[str, list[str]]:
    rows = db.query(TagVocabulary).filter(TagVocabulary.status == "approved").all()
    out: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    for r in rows:
        out.setdefault(r.category, []).append(r.value)
    return out


def validate(db: Session, category: str, value: str) -> TagVocabulary | None:
    """Return the approved vocabulary row for (category, value), or None."""
    if category not in CATEGORIES:
        return None
    return (
        db.query(TagVocabulary)
        .filter(TagVocabulary.category == category, TagVocabulary.value == value, TagVocabulary.status == "approved")
        .first()
    )


def propose_term(db: Session, category: str, value: str, proposed_by: uuid.UUID | None) -> TagVocabulary:
    """Record an unknown-but-plausible term as a pending_approval proposal.
    Never applied to a document until governance approves it."""
    existing = (
        db.query(TagVocabulary).filter(TagVocabulary.category == category, TagVocabulary.value == value).first()
    )
    if existing is not None:
        return existing
    term = TagVocabulary(category=category, value=value, status="pending_approval", proposed_by=proposed_by)
    db.add(term)
    db.flush()
    return term


def apply_tags(db: Session, run_id: uuid.UUID, resolved: list[tuple[TagVocabulary, float, str]]) -> None:
    """Write confirmed tags to run_tags. resolved = [(vocab_row, confidence, status)]."""
    for vocab, confidence, status in resolved:
        db.add(RunTag(run_id=run_id, tag_id=vocab.id, confidence=confidence, status=status))
    db.flush()
