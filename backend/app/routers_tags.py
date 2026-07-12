import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.permissions import has_capability, require_capability
from app.db import get_db
from app.models import RunTag, TagVocabulary, User
from app.schemas import RunTagOut, TagCreateRequest, TagVocabularyOut
from app.tags.service import CATEGORIES

router = APIRouter(prefix="/tags", tags=["tags"])


@router.get("/vocabulary", response_model=list[TagVocabularyOut])
def list_vocabulary(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[TagVocabularyOut]:
    rows = db.query(TagVocabulary).order_by(TagVocabulary.category, TagVocabulary.value).all()
    return [TagVocabularyOut(id=r.id, category=r.category, value=r.value, status=r.status, created_at=r.created_at) for r in rows]


@router.get("/proposals", response_model=list[TagVocabularyOut])
def list_proposals(
    _: User = Depends(require_capability("manage_tag_taxonomy")), db: Session = Depends(get_db)
) -> list[TagVocabularyOut]:
    rows = db.query(TagVocabulary).filter(TagVocabulary.status == "pending_approval").order_by(TagVocabulary.created_at).all()
    return [TagVocabularyOut(id=r.id, category=r.category, value=r.value, status=r.status, created_at=r.created_at) for r in rows]


@router.post("/vocabulary", response_model=TagVocabularyOut, status_code=status.HTTP_201_CREATED)
def add_or_propose_term(payload: TagCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> TagVocabularyOut:
    category, value = payload.category, payload.value.strip().lower()
    if category not in CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown category '{category}'")

    can_manage = has_capability(user, "manage_tag_taxonomy")
    can_propose = has_capability(user, "propose_tag_taxonomy")
    if not (can_manage or can_propose):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot manage or propose tags")

    if db.query(TagVocabulary).filter(TagVocabulary.category == category, TagVocabulary.value == value).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Term already exists")

    # Governance adds approved terms directly; practice-leads propose (pending).
    term = TagVocabulary(
        category=category,
        value=value,
        status="approved" if can_manage else "pending_approval",
        proposed_by=user.id,
    )
    db.add(term)
    db.commit()
    db.refresh(term)
    return TagVocabularyOut(id=term.id, category=term.category, value=term.value, status=term.status, created_at=term.created_at)


@router.post("/vocabulary/{term_id}/approve", response_model=TagVocabularyOut)
def approve_term(term_id: uuid.UUID, _: User = Depends(require_capability("manage_tag_taxonomy")), db: Session = Depends(get_db)) -> TagVocabularyOut:
    term = db.get(TagVocabulary, term_id)
    if term is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Term not found")
    term.status = "approved"
    db.commit()
    db.refresh(term)
    return TagVocabularyOut(id=term.id, category=term.category, value=term.value, status=term.status, created_at=term.created_at)


@router.delete("/vocabulary/{term_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_term(term_id: uuid.UUID, _: User = Depends(require_capability("manage_tag_taxonomy")), db: Session = Depends(get_db)) -> None:
    term = db.get(TagVocabulary, term_id)
    if term is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Term not found")
    db.delete(term)
    db.commit()


@router.get("/runs/{run_id}", response_model=list[RunTagOut])
def run_tags(run_id: uuid.UUID, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[RunTagOut]:
    rows = (
        db.query(RunTag, TagVocabulary)
        .join(TagVocabulary, RunTag.tag_id == TagVocabulary.id)
        .filter(RunTag.run_id == run_id)
        .all()
    )
    return [RunTagOut(category=v.category, value=v.value, confidence=rt.confidence, status=rt.status) for rt, v in rows]
