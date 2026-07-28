import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload

from app.auth.permissions import require_capability
from app.db import get_db
from app.masking import dictionary
from app.models import AccountOwnership, AgentRun, AuditLog, ClientAccount, LogoReference, MaskingEntity, Role, User
from app.schemas import (
    AuditLogEntryOut,
    ClientAccountCreateRequest,
    ClientAccountOut,
    LogoReferenceOut,
    MaskingEntityOut,
    OwnerAssignRequest,
    UserOut,
)

router = APIRouter(prefix="/governance", tags=["governance"])


def _to_account_out(account: ClientAccount) -> ClientAccountOut:
    return ClientAccountOut(
        id=account.id,
        name=account.name,
        created_at=account.created_at,
        owners=[
            UserOut(id=o.user.id, email=o.user.email, role=o.user.role.name, created_at=o.user.created_at)
            for o in account.owners
        ],
    )


@router.get("/accounts", response_model=list[ClientAccountOut])
def list_accounts(
    _: User = Depends(require_capability("assign_account_ownership")),
    db: Session = Depends(get_db),
) -> list[ClientAccountOut]:
    accounts = (
        db.query(ClientAccount)
        .options(joinedload(ClientAccount.owners).joinedload(AccountOwnership.user))
        .order_by(ClientAccount.name)
        .all()
    )
    return [_to_account_out(a) for a in accounts]


@router.get("/practice-leads", response_model=list[UserOut])
def list_practice_leads(
    _: User = Depends(require_capability("assign_account_ownership")),
    db: Session = Depends(get_db),
) -> list[UserOut]:
    users = db.query(User).join(Role).filter(Role.name == "practice_lead").order_by(User.email).all()
    return [UserOut(id=u.id, email=u.email, role=u.role.name, created_at=u.created_at) for u in users]


@router.post("/accounts", response_model=ClientAccountOut, status_code=status.HTTP_201_CREATED)
def create_account(
    payload: ClientAccountCreateRequest,
    actor: User = Depends(require_capability("assign_account_ownership")),
    db: Session = Depends(get_db),
) -> ClientAccountOut:
    if db.query(ClientAccount).filter(ClientAccount.name == payload.name).first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this name already exists")

    account = ClientAccount(name=payload.name)
    db.add(account)
    db.add(AuditLog(actor_id=actor.id, action=f"client_account_created: {payload.name}"))
    db.commit()
    db.refresh(account)
    return _to_account_out(account)


@router.post("/accounts/{account_id}/owners", response_model=ClientAccountOut)
def assign_owner(
    account_id: uuid.UUID,
    payload: OwnerAssignRequest,
    actor: User = Depends(require_capability("assign_account_ownership")),
    db: Session = Depends(get_db),
) -> ClientAccountOut:
    account = db.get(ClientAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    target_user = db.get(User, payload.user_id)
    if target_user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if target_user.role.name != "practice_lead":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Account ownership can only be assigned to a practice_lead user")

    already_owner = (
        db.query(AccountOwnership)
        .filter(AccountOwnership.client_account_id == account.id, AccountOwnership.user_id == target_user.id)
        .first()
    )
    if already_owner is None:
        db.add(AccountOwnership(user_id=target_user.id, client_account_id=account.id))
        db.add(AuditLog(actor_id=actor.id, action=f"account_owner_assigned: {target_user.email} -> {account.name}"))
        db.commit()

    db.refresh(account)
    return _to_account_out(account)


@router.delete("/accounts/{account_id}/owners/{user_id}", response_model=ClientAccountOut)
def remove_owner(
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: User = Depends(require_capability("assign_account_ownership")),
    db: Session = Depends(get_db),
) -> ClientAccountOut:
    account = db.get(ClientAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    ownership = (
        db.query(AccountOwnership)
        .filter(AccountOwnership.client_account_id == account_id, AccountOwnership.user_id == user_id)
        .first()
    )
    if ownership is not None:
        target_email = ownership.user.email
        db.delete(ownership)
        db.add(AuditLog(actor_id=actor.id, action=f"account_owner_removed: {target_email} from {account.name}"))
        db.commit()

    db.refresh(account)
    return _to_account_out(account)


@router.get("/audit-log", response_model=list[AuditLogEntryOut])
def get_full_audit_log(
    _: User = Depends(require_capability("view_audit_log_full")),
    db: Session = Depends(get_db),
) -> list[AuditLogEntryOut]:
    entries = db.query(AuditLog).order_by(AuditLog.created_at.desc()).all()
    return [
        AuditLogEntryOut(
            id=e.id,
            run_id=e.run_id,
            actor_id=e.actor_id,
            actor_email=e.actor_id and db.get(User, e.actor_id).email,
            action=e.action,
            created_at=e.created_at,
        )
        for e in entries
    ]


# A pending_approval entry older than this is a forgotten decision: it keeps
# re-surfacing in every run's proposal until someone approves or skips it,
# which is exactly how a stray "[REDACTED] gray box"-class entity silently
# accumulated before the dictionary view existed. Surfaced as a badge, not
# auto-actioned - the decision itself still belongs to governance.
DICTIONARY_STALE_AFTER_DAYS = 14


def _to_entity_out(entity: MaskingEntity, logos_by_entity: dict[uuid.UUID, list[LogoReference]] | None = None) -> MaskingEntityOut:
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=DICTIONARY_STALE_AFTER_DAYS)
    logos = (logos_by_entity or {}).get(entity.id, [])
    return MaskingEntityOut(
        id=entity.id,
        mask_token=entity.mask_token,
        entity_type=entity.entity_type,
        status=entity.status,
        aliases=[a.raw_value for a in entity.aliases],
        client_account_id=entity.client_account_id,
        client_account_name=entity.client_account.name if entity.client_account else None,
        created_at=entity.created_at,
        stale=entity.status == "pending_approval" and entity.created_at < stale_cutoff,
        logos=[
            LogoReferenceOut(id=lr.id, created_at=lr.created_at, thumbnail_available=bool(lr.thumbnail_path))
            for lr in logos
        ],
    )


@router.get("/masking-dictionary", response_model=list[MaskingEntityOut])
def list_masking_dictionary(
    _: User = Depends(require_capability("view_raw_masking_dictionary")),
    db: Session = Depends(get_db),
) -> list[MaskingEntityOut]:
    entities = (
        db.query(MaskingEntity)
        .options(joinedload(MaskingEntity.aliases), joinedload(MaskingEntity.client_account))
        .order_by(MaskingEntity.created_at.desc())
        .all()
    )
    logos_by_entity: dict[uuid.UUID, list[LogoReference]] = {}
    for lr in db.query(LogoReference).order_by(LogoReference.created_at.desc()).all():
        logos_by_entity.setdefault(lr.mask_entity_id, []).append(lr)
    return [_to_entity_out(e, logos_by_entity) for e in entities]


@router.get("/logo-references/{logo_reference_id}/thumbnail")
def get_logo_reference_thumbnail(
    logo_reference_id: int,
    _: User = Depends(require_capability("view_raw_masking_dictionary")),
    db: Session = Depends(get_db),
):
    """Preview PNG of the actual approved image a mask token was matched
    against (see app.masking.logo_reference) - not every reference has one
    (best-effort, see LogoReference.thumbnail_path's docstring)."""
    ref = db.get(LogoReference, logo_reference_id)
    if ref is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Logo reference not found")
    if not ref.thumbnail_path or not os.path.exists(ref.thumbnail_path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No thumbnail available for this logo reference")
    return FileResponse(ref.thumbnail_path, media_type="image/png")


@router.post("/masking-dictionary/{entity_id}/skip", response_model=MaskingEntityOut)
def skip_masking_entity(
    entity_id: uuid.UUID,
    _: User = Depends(require_capability("approve_new_mask")),
    db: Session = Depends(get_db),
) -> MaskingEntityOut:
    entity = db.get(MaskingEntity, entity_id)
    if entity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entity not found")
    dictionary.skip(db, entity)
    db.commit()
    db.refresh(entity)
    return _to_entity_out(entity)


@router.post("/masking-dictionary/{entity_id}/unskip", response_model=MaskingEntityOut)
def unskip_masking_entity(
    entity_id: uuid.UUID,
    _: User = Depends(require_capability("approve_new_mask")),
    db: Session = Depends(get_db),
) -> MaskingEntityOut:
    entity = db.get(MaskingEntity, entity_id)
    if entity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entity not found")
    dictionary.unskip(db, entity)
    db.commit()
    db.refresh(entity)
    return _to_entity_out(entity)


@router.get("/review-deltas")
def review_deltas_summary(
    _: User = Depends(require_capability("view_raw_masking_dictionary")),
    db: Session = Depends(get_db),
) -> dict:
    """Aggregates Task 3's reviewer-edit deltas (recall/precision misses, per
    entity_type) across every Sanitization run that has them - the cheapest
    real accuracy signal this system produces, previously just sitting
    unused inside each run's own output_json. Intended to inform tuning
    SANITIZATION_CONFIDENCE_THRESHOLDS (config.py) against real reviewed
    usage instead of the untuned 0.6 default every type currently shares."""
    runs = db.query(AgentRun).filter(AgentRun.agent_id == "sanitization", AgentRun.output_json.isnot(None)).all()

    recall_totals: dict[str, int] = {}
    precision_totals: dict[str, int] = {}
    runs_with_data = 0
    for run in runs:
        deltas = (run.output_json or {}).get("review_deltas") or {}
        recall = deltas.get("recall_miss_by_type") or {}
        precision = deltas.get("precision_miss_by_type") or {}
        if not recall and not precision:
            continue
        runs_with_data += 1
        for etype, count in recall.items():
            recall_totals[etype] = recall_totals.get(etype, 0) + count
        for etype, count in precision.items():
            precision_totals[etype] = precision_totals.get(etype, 0) + count

    entity_types = sorted(set(recall_totals) | set(precision_totals))
    return {
        "runs_with_data": runs_with_data,
        "total_runs_checked": len(runs),
        "by_entity_type": [
            {
                "entity_type": etype,
                "recall_misses": recall_totals.get(etype, 0),
                "precision_misses": precision_totals.get(etype, 0),
            }
            for etype in entity_types
        ],
    }
