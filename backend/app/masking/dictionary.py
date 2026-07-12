"""Masking dictionary — PROTECTED calls (agent code only, never the model).

Owns global, stable mask tokens: one canonical entity per client identifier,
one token reused across every document (decision: global mask scope). New
entities are created as pending_approval and promoted on reviewer approval.
"""

import re
import uuid

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MaskingAlias, MaskingEntity

settings = get_settings()

_TOKEN_PREFIX = {
    "CLIENT_NAME": "CLIENT",
    "CLIENT_PERSON": "CLIENT_PERSON",
    "CLIENT_LOCATION": "CLIENT_LOCATION",
    "CLIENT_EMAIL_DOMAIN": "CLIENT_DOMAIN",
    "CLIENT_SYSTEM_NAME": "CLIENT_SYSTEM",
    "CLIENT_CONTRACT_ID": "CLIENT_CONTRACT",
}


def normalize(raw_value: str) -> str:
    """Match key: lowercased, punctuation-stripped, whitespace-collapsed."""
    v = raw_value.lower().strip()
    v = re.sub(r"[^\w@.\s-]", "", v)
    v = re.sub(r"\s+", " ", v)
    return v


def is_own_firm(surface: str) -> bool:
    """True if `surface` names the delivery firm itself, not a client - checked
    as "does the normalized name CONTAIN this token", not a bare prefix (a
    prefix match on e.g. "navi" would also wrongly exclude unrelated real
    companies that happen to start with the same letters)."""
    key = re.sub(r"[^a-z0-9]", "", surface.lower())
    return any(re.sub(r"[^a-z0-9]", "", name.lower()) in key for name in settings.OWN_FIRM_NAMES if name.strip())


def lookup(db: Session, raw_value: str) -> MaskingEntity | None:
    """Deterministic pass: is this surface form already a known alias?"""
    key = normalize(raw_value)
    alias = db.query(MaskingAlias).filter(MaskingAlias.normalized_key == key).first()
    return alias.entity if alias else None


def is_skipped(db: Session, raw_value: str) -> bool:
    """True if a reviewer has explicitly marked this surface as never worth
    proposing again (governance decision via the masking dictionary view) -
    same "stop asking about this every run" effect as is_own_firm, but
    per-term and reviewer-controlled instead of hardcoded to firm names."""
    entity = lookup(db, raw_value)
    return entity is not None and entity.status == "skipped"


def _next_token(db: Session, entity_type: str) -> str:
    prefix = _TOKEN_PREFIX.get(entity_type, "CLIENT")
    # Count existing entities that share this token prefix to pick the next index.
    like = f"[{prefix}_%]"
    n = db.query(MaskingEntity).filter(MaskingEntity.mask_token.like(like)).count()
    # Ensure uniqueness even if counts and tokens drift.
    idx = n + 1
    while db.query(MaskingEntity).filter(MaskingEntity.mask_token == f"[{prefix}_{idx}]").first():
        idx += 1
    return f"[{prefix}_{idx}]"


def get_or_create(
    db: Session,
    raw_value: str,
    entity_type: str,
    run_id: uuid.UUID,
    approved: bool = False,
) -> MaskingEntity:
    """Return the canonical entity for a surface form, creating it (and its
    alias) if unseen. New entities are pending_approval unless approved=True."""
    existing = lookup(db, raw_value)
    if existing is not None:
        return existing

    entity = MaskingEntity(
        entity_type=entity_type,
        mask_token=_next_token(db, entity_type),
        status="approved" if approved else "pending_approval",
        created_by_run_id=run_id,
    )
    db.add(entity)
    db.flush()  # assign id
    db.add(MaskingAlias(entity_id=entity.id, raw_value=raw_value, normalized_key=normalize(raw_value)))
    db.flush()
    return entity


def add_alias(db: Session, entity: MaskingEntity, raw_value: str) -> None:
    key = normalize(raw_value)
    if db.query(MaskingAlias).filter(MaskingAlias.normalized_key == key).first():
        return
    db.add(MaskingAlias(entity_id=entity.id, raw_value=raw_value, normalized_key=key))
    db.flush()


def approve(db: Session, entity: MaskingEntity, client_account_id: uuid.UUID | None = None) -> None:
    """Promote a pending entity to approved on reviewer sign-off (governance)."""
    entity.status = "approved"
    if client_account_id is not None:
        entity.client_account_id = client_account_id
    db.flush()


def skip(db: Session, entity: MaskingEntity) -> None:
    """Governance decision: never propose this term again (see is_skipped)."""
    entity.status = "skipped"
    db.flush()


def unskip(db: Session, entity: MaskingEntity) -> None:
    """Reverse a skip decision - back to pending_approval, so it can be
    proposed (and reviewed normally) again."""
    entity.status = "pending_approval"
    db.flush()
